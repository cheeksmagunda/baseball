"""Verify that every (date, player_name, team) row in our historical CSVs
matches the team the player actually played for that day, per MLB Stats API.

For each game in /data/historical_slate_results.json (which now has gamePk
populated), fetches the boxscore once and builds a (date, normalized_name)
→ {teams the player appeared on} lookup.  Then walks all four CSV-style
historical files and flags any row whose `team` disagrees with the lookup.

Idempotent + read-only by default (no edits).  Use --auto-fix to rewrite
the CSV's `team` column for any row whose name appears on exactly ONE team
in the day's boxscores (high-confidence single-source-of-truth update).

Output: a per-file diff summary with player name, our team, MLB team,
date.  Catches the Weathers MIA→NYY / Julien MIN→COL class of manual errors.

Usage:
    .venv-scraper/bin/python scripts/verify_player_teams.py
    .venv-scraper/bin/python scripts/verify_player_teams.py --auto-fix
"""
import argparse
import csv
import json
import logging
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SLATE_RESULTS = DATA / "historical_slate_results.json"
PLAYERS_CSV = DATA / "historical_players.csv"
DRAFTS_CSV = DATA / "historical_winning_drafts.csv"
HV_STATS_CSV = DATA / "hv_player_game_stats.csv"

MLB_API = "https://statsapi.mlb.com/api/v1"
MLB_TEAM_ID_TO_ABBR_OVERRIDE = {133: "ATH"}  # API returns "OAK" for Athletics
HTTP_TIMEOUT = 20
MAX_WORKERS = 10

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("verify")

TEAM_ALIASES = {
    "KCR": "KC", "CHW": "CWS", "AZ": "ARI", "WSN": "WSH",
    "TBR": "TB", "SDP": "SD", "SFG": "SF", "OAK": "ATH",
}

def canon(team: str) -> str:
    return TEAM_ALIASES.get(team.strip().upper(), team.strip().upper())


def normalize_name(s: str) -> str:
    """ASCII-fold, lowercase, collapse whitespace; strip punctuation except '.' for initials."""
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_name.lower().split())


def name_match(a: str, b: str) -> bool:
    """True if two names match — handles 'A. Judge' vs 'Aaron Judge'."""
    na, nb = normalize_name(a), normalize_name(b)
    if na == nb:
        return True
    # Try initial+surname match: "A. Judge" → "a. judge"; check against "aaron judge"
    pa = re.match(r"^([a-z])\.\s+(.+)$", na)
    pb = re.match(r"^([a-z])\.\s+(.+)$", nb)
    if pa and not pb:
        return nb.startswith(pa.group(1)) and nb.split(" ", 1)[-1] == pa.group(2)
    if pb and not pa:
        return na.startswith(pb.group(1)) and na.split(" ", 1)[-1] == pb.group(2)
    return False


def fetch_boxscore_players(game_pk: int) -> dict[str, str]:
    """Return {normalized_player_name: canonical_team_abbr} for one boxscore."""
    if not game_pk:
        return {}
    url = f"{MLB_API}/game/{game_pk}/boxscore"
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    boxscore_teams = data.get("teams", {})
    out: dict[str, str] = {}
    for side in ("home", "away"):
        team_data = boxscore_teams.get(side, {})
        team_id = team_data.get("team", {}).get("id")
        team_abbr_raw = team_data.get("team", {}).get("abbreviation", "")
        team_abbr = MLB_TEAM_ID_TO_ABBR_OVERRIDE.get(team_id, canon(team_abbr_raw))
        for pid, p in team_data.get("players", {}).items():
            full_name = p.get("person", {}).get("fullName", "")
            if full_name:
                out[normalize_name(full_name)] = team_abbr
    return out


def build_player_lookup() -> tuple[dict[tuple[str, str], set[str]], dict[str, set[str]]]:
    """Returns (player_lookup, teams_playing_lookup).

    player_lookup: (date, normalized_name) → set of teams the name appears on.
    teams_playing_lookup: date → set of teams that played that day.  Used to
    detect DNP-correct rows: if our team didn't play that day, the player
    can't be in any boxscore even if they're correctly listed on that team.
    """
    envs = json.loads(SLATE_RESULTS.read_text())
    tasks = []
    for env in envs:
        date = env["date"]
        for g in env.get("games", []):
            gpk = g.get("game_pk")
            if gpk:
                tasks.append((date, gpk))

    log.info(f"Fetching {len(tasks)} boxscores in parallel ...")
    player_lookup: dict[tuple[str, str], set[str]] = {}
    teams_playing: dict[str, set[str]] = {}

    def fetch_one(args):
        date, gpk = args
        try:
            return date, fetch_boxscore_players(gpk)
        except Exception as e:
            log.warning(f"  {date} game_pk={gpk} failed: {e}")
            return date, {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for date, players in ex.map(fetch_one, tasks):
            for nname, team in players.items():
                key = (date, nname)
                player_lookup.setdefault(key, set()).add(team)
                teams_playing.setdefault(date, set()).add(team)
    n_collisions = sum(1 for v in player_lookup.values() if len(v) > 1)
    log.info(f"  built lookup: {len(player_lookup)} (date, name) entries, "
             f"{n_collisions} same-day name collisions")
    return player_lookup, teams_playing


def check_csv(path: Path, name_col: str, team_col: str,
              player_lookup: dict[tuple[str, str], set[str]],
              teams_playing: dict[str, set[str]],
              auto_fix: bool):
    """Walk a CSV and report (or fix) team mismatches."""
    rows = list(csv.DictReader(open(path)))
    if not rows:
        return
    fields = list(rows[0].keys())
    confirmed: list[tuple] = []  # (date, name, our_team, actual_team) — single source of truth
    ambiguous: list[tuple] = []  # (date, name, our_team, possible_teams) — collision; left alone
    dnp_skipped = 0  # our team didn't play this date — can't auto-fix from boxscores
    fixed = 0
    not_found = 0
    for r in rows:
        date = r.get("date", "")
        name = r.get(name_col, "")
        our_team = canon(r.get(team_col, ""))
        if not date or not name:
            continue
        nname = normalize_name(name)
        actual_set: set[str] | None = player_lookup.get((date, nname))
        if actual_set is None:
            for (d, n), t_set in player_lookup.items():
                if d == date and name_match(name, n):
                    actual_set = t_set
                    break
        if actual_set is None:
            not_found += 1
            continue
        if our_team in actual_set:
            continue  # match (possibly disambiguated by being in the set)
        # If our team didn't play this date, the player simply wasn't in
        # any boxscore — could be a DNP day (player benched + same name
        # on another team played).  Skip rather than auto-fix.
        if our_team not in teams_playing.get(date, set()):
            dnp_skipped += 1
            continue
        if len(actual_set) == 1:
            actual_team = next(iter(actual_set))
            confirmed.append((date, name, our_team, actual_team))
            if auto_fix:
                r[team_col] = actual_team
                fixed += 1
        else:
            ambiguous.append((date, name, our_team, sorted(actual_set)))

    if auto_fix and fixed:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    print(f"\n=== {path.name} ===")
    print(f"  rows: {len(rows)}  confirmed wrong: {len(confirmed)}  "
          f"ambiguous: {len(ambiguous)}  DNP-skipped: {dnp_skipped}  "
          f"not-found: {not_found}")
    if auto_fix:
        print(f"  AUTO-FIXED: {fixed} rows")
    if confirmed:
        print(f"  Confirmed wrong (auto-fixable):")
        for date, name, our_team, actual_team in confirmed[:30]:
            print(f"    {date}  {name:30s}  ours={our_team:>3}  actual={actual_team:>3}")
        if len(confirmed) > 30:
            print(f"    ... and {len(confirmed) - 30} more")
    if ambiguous:
        print(f"  Ambiguous (multiple possible teams — manual review):")
        for date, name, our_team, possible in ambiguous[:10]:
            print(f"    {date}  {name:30s}  ours={our_team:>3}  possible={possible}")
        if len(ambiguous) > 10:
            print(f"    ... and {len(ambiguous) - 10} more")
    return len(confirmed)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--auto-fix", action="store_true",
                    help="rewrite team column for high-confidence matches")
    args = ap.parse_args()

    player_lookup, teams_playing = build_player_lookup()
    total_mismatches = 0
    total_mismatches += check_csv(PLAYERS_CSV, "player_name", "team",
                                  player_lookup, teams_playing, args.auto_fix)
    total_mismatches += check_csv(DRAFTS_CSV, "player_name", "team",
                                  player_lookup, teams_playing, args.auto_fix)
    total_mismatches += check_csv(HV_STATS_CSV, "player_name", "team_actual",
                                  player_lookup, teams_playing, args.auto_fix)
    print(f"\nTotal mismatches across all 3 files: {total_mismatches}")


if __name__ == "__main__":
    main()
