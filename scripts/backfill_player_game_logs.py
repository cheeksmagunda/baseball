"""Backfill 10-game pre-slate windows for every player in historical_players.csv.

The live T-65 pipeline reads the most recent ~10 game logs per player to
compute the `recent_form` and `hot_streak` trait sub-signals (see
`app/services/scoring_engine.py::score_recent_form` and
`score_hot_streak`).  CLAUDE.md V16 Phase 2 explicitly flags this as the
calibration gap that's been left unaddressed:

    The harness measurement was on a CSV-only proxy (skipping game-log-
    derived recent_form / hot_streak that the live runtime computes).

This script closes the gap.  For every (slate_date, player) pair in
`data/historical_players.csv`, it pulls the player's full season game-log
from MLB Stats API once, then for each slate_date the player appears on
takes the 10 most-recent games STRICTLY BEFORE that slate_date.

Output
------
New file: `data/historical_player_game_logs.csv`.
Columns:
  slate_date, player_name, team, mlb_id, position,
  game_date, opponent, is_home,
  ab, runs, hits, hr, rbi, bb, so, sb,
  ip, er, k_pitching, decision

For batter rows: pitching columns left blank.
For pitcher rows: batting columns left blank.

Caching
-------
The MLB API gameLog response per (player, season) is cached to
`scripts/output/.gamelog_cache/{mlb_id}.json` so a re-run touches the
network only for newly-resolved players.  ~1500 unique players × ~50 KB
each = ~75 MB cache.

Idempotent
----------
A re-run skips (slate_date, mlb_id) pairs already present in the output
CSV.  Use `--force` to rewrite all rows.

Calibration-only.  This script only reads + writes /data/ and
/scripts/output/ files; never touches the live pipeline DB or scoring
engine.

Source
------
    /people/{id}/stats?stats=gameLog&group=hitting,pitching&season=YYYY

Usage
-----
    python scripts/backfill_player_game_logs.py
    python scripts/backfill_player_game_logs.py --dry-run
    python scripts/backfill_player_game_logs.py --force
    python scripts/backfill_player_game_logs.py --window 10
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
HISTORICAL_PLAYERS = ROOT / "data" / "historical_players.csv"
GAME_LOGS_CSV = ROOT / "data" / "historical_player_game_logs.csv"
CACHE_DIR = ROOT / "scripts" / "output" / ".gamelog_cache"
MLB_API = "https://statsapi.mlb.com/api/v1"
HTTP_TIMEOUT = 30
MAX_WORKERS = 16
DEFAULT_WINDOW = 10

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

sys.path.insert(0, str(ROOT))
from scripts.backfill_player_season_stats_at_slate import resolve_mlb_id  # noqa: E402

# Same canon as elsewhere — historical "AZ" → "ARI", etc.
TEAM_ABBR_ALIASES = {
    "KCR": "KC", "CHW": "CWS", "AZ": "ARI", "WSN": "WSH",
    "TBR": "TB", "SDP": "SD", "SFG": "SF", "OAK": "ATH",
}


def _canon_team(team: str) -> str:
    return TEAM_ABBR_ALIASES.get(team.strip().upper(), team.strip().upper())


def _is_pitcher_position(pos: str) -> bool:
    return (pos or "").upper() in {"P", "SP", "RP"}


def _safe_int(v) -> int:
    if v in (None, "", "-"):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _safe_float(v) -> float | None:
    if v in (None, "", "-", ".---"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Game-log fetch + cache
# ---------------------------------------------------------------------------

def _cache_path(mlb_id: int, season: int) -> Path:
    return CACHE_DIR / f"{mlb_id}_{season}.json"


def fetch_game_log(mlb_id: int, season: int) -> dict:
    """Return parsed gameLog dict from MLB API.  Caches to disk per (mlb_id, season).

    Format: {'hitting': [{date, opp_abbr, is_home, ab, runs, hits, hr, rbi,
                          bb, so, sb}, ...],
             'pitching': [{date, opp_abbr, is_home, ip, er, k_pitching,
                           decision}, ...]}.
    """
    cache_file = _cache_path(mlb_id, season)
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass  # Corrupted cache; re-fetch.

    url = (
        f"{MLB_API}/people/{mlb_id}/stats"
        f"?stats=gameLog&group=hitting,pitching&season={season}"
    )
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"gameLog fetch failed for mlb_id={mlb_id} season={season}: {e}")
        return {"hitting": [], "pitching": []}

    out: dict[str, list[dict]] = {"hitting": [], "pitching": []}
    for group in data.get("stats", []) or []:
        kind = group.get("group", {}).get("displayName", "")
        if kind not in ("hitting", "pitching"):
            continue
        for split in group.get("splits", []) or []:
            gs = split.get("stat", {}) or {}
            opp_team = split.get("opponent", {}) or {}
            row = {
                "date": split.get("date", ""),
                "opp_id": opp_team.get("id"),
                "is_home": bool(split.get("isHome")),
            }
            if kind == "hitting":
                row.update(
                    ab=_safe_int(gs.get("atBats")),
                    runs=_safe_int(gs.get("runs")),
                    hits=_safe_int(gs.get("hits")),
                    hr=_safe_int(gs.get("homeRuns")),
                    rbi=_safe_int(gs.get("rbi")),
                    bb=_safe_int(gs.get("baseOnBalls")),
                    so=_safe_int(gs.get("strikeOuts")),
                    sb=_safe_int(gs.get("stolenBases")),
                )
            else:
                ip_val = _safe_float(gs.get("inningsPitched"))
                row.update(
                    ip=ip_val if ip_val is not None else 0.0,
                    er=_safe_int(gs.get("earnedRuns")),
                    k_pitching=_safe_int(gs.get("strikeOuts")),
                    decision=(gs.get("decision") or split.get("isWin") and "W" or "") or "",
                )
            out[kind].append(row)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cache_file.write_text(json.dumps(out))
    except Exception as e:
        log.warning(f"failed to cache gameLog for mlb_id={mlb_id}: {e}")
    return out


# ---------------------------------------------------------------------------
# MLB team-id → abbr (for opponent column).  Mirrors app.core.mlb_api but kept
# inline so this script doesn't import the runtime module.
# ---------------------------------------------------------------------------

TEAM_ID_TO_ABBR = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHC", 113: "CIN",
    114: "CLE", 115: "COL", 116: "DET", 117: "HOU", 118: "KC",  119: "LAD",
    120: "WSH", 121: "NYM", 133: "ATH", 134: "PIT", 135: "SD",  136: "SEA",
    137: "SF",  138: "STL", 139: "TB",  140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}


def _opp_abbr(opp_id) -> str:
    if opp_id in (None, ""):
        return ""
    try:
        return TEAM_ID_TO_ABBR.get(int(opp_id), "")
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

OUT_FIELDNAMES = [
    "slate_date", "player_name", "team", "mlb_id", "position",
    "game_date", "opponent", "is_home",
    "ab", "runs", "hits", "hr", "rbi", "bb", "so", "sb",
    "ip", "er", "k_pitching", "decision",
]


def _read_existing_pairs(path: Path) -> set[tuple[str, int]]:
    """Return set of (slate_date, mlb_id) already present so re-runs are
    idempotent."""
    if not path.exists():
        return set()
    out: set[tuple[str, int]] = set()
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                out.add((row.get("slate_date", ""), int(row.get("mlb_id", "0"))))
            except (TypeError, ValueError):
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill 10-game pre-slate windows into historical_player_game_logs.csv"
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Rebuild output CSV from scratch (don't skip already-written pairs)")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                    help=f"Pre-slate game-window size (default {DEFAULT_WINDOW})")
    args = ap.parse_args()

    with HISTORICAL_PLAYERS.open(newline="") as f:
        in_rows = list(csv.DictReader(f))
    log.info(f"{HISTORICAL_PLAYERS.name}: {len(in_rows)} rows")

    existing_pairs = set() if args.force else _read_existing_pairs(GAME_LOGS_CSV)
    log.info(f"Already-written (slate_date, mlb_id) pairs: {len(existing_pairs)}")

    # Resolve mlb_id per (player_name, team).  Group rows by mlb_id so each
    # player's gameLog is fetched once across all slates they appear on.
    by_player: dict[int, list[dict]] = {}  # mlb_id → list of in_rows
    skipped_already = 0
    skipped_no_id = 0

    for row in in_rows:
        slate_date = row.get("date", "")
        if not slate_date:
            continue
        player_name = row.get("player_name", "")
        team = _canon_team(row.get("team") or "")
        mlb_id = resolve_mlb_id(player_name, team)
        if mlb_id is None:
            skipped_no_id += 1
            continue
        if (slate_date, mlb_id) in existing_pairs:
            skipped_already += 1
            continue
        by_player.setdefault(mlb_id, []).append({**row, "_team_canon": team, "_mlb_id": mlb_id})

    todo_pairs = sum(len(v) for v in by_player.values())
    log.info(
        f"Players to fetch: {len(by_player)} | (slate, mlb_id) pairs to write: {todo_pairs} | "
        f"skipped_already={skipped_already} unresolved_id={skipped_no_id}"
    )

    if args.dry_run:
        log.info("--dry-run, no fetch, no write")
        return 0

    # Open the output CSV in append mode if not --force, else recreate.
    write_header = args.force or not GAME_LOGS_CSV.exists()
    mode = "w" if args.force else "a"
    out_f = GAME_LOGS_CSV.open(mode, newline="")
    writer = csv.DictWriter(out_f, fieldnames=OUT_FIELDNAMES)
    if write_header:
        writer.writeheader()

    populated_rows = 0
    no_log_players = 0
    t0 = time.time()

    def _work(item: tuple[int, list[dict]]):
        mlb_id, rows_for_player = item
        # Fetch ALL seasons that appear in this player's slates.  In practice
        # the corpus is one season (2026) but we honour multi-season inputs.
        seasons = {int(r["date"].split("-", 1)[0]) for r in rows_for_player}
        merged: dict[str, list[dict]] = {"hitting": [], "pitching": []}
        for s in seasons:
            gl = fetch_game_log(mlb_id, s)
            merged["hitting"].extend(gl.get("hitting", []))
            merged["pitching"].extend(gl.get("pitching", []))
        # Sort once by date ascending.
        merged["hitting"].sort(key=lambda x: x.get("date", ""))
        merged["pitching"].sort(key=lambda x: x.get("date", ""))
        return (mlb_id, rows_for_player, merged)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(_work, item) for item in by_player.items()]
        for fut in as_completed(futures):
            mlb_id, rows_for_player, merged = fut.result()
            if not merged["hitting"] and not merged["pitching"]:
                no_log_players += 1
                continue

            for r in rows_for_player:
                slate_date = r["date"]
                is_pitcher = _is_pitcher_position(r.get("position", ""))
                source = merged["pitching"] if is_pitcher else merged["hitting"]
                # Take last `window` games strictly BEFORE slate_date.
                prior = [g for g in source if g.get("date", "") < slate_date]
                window = prior[-args.window:]
                for g in window:
                    base = {
                        "slate_date": slate_date,
                        "player_name": r.get("player_name", ""),
                        "team": r.get("_team_canon", ""),
                        "mlb_id": str(mlb_id),
                        "position": r.get("position", ""),
                        "game_date": g.get("date", ""),
                        "opponent": _opp_abbr(g.get("opp_id")),
                        "is_home": "1" if g.get("is_home") else "0",
                        "ab": "", "runs": "", "hits": "", "hr": "",
                        "rbi": "", "bb": "", "so": "", "sb": "",
                        "ip": "", "er": "", "k_pitching": "", "decision": "",
                    }
                    if is_pitcher:
                        base["ip"] = f"{g.get('ip', 0.0):.1f}"
                        base["er"] = str(g.get("er", 0))
                        base["k_pitching"] = str(g.get("k_pitching", 0))
                        base["decision"] = g.get("decision", "")
                    else:
                        base["ab"] = str(g.get("ab", 0))
                        base["runs"] = str(g.get("runs", 0))
                        base["hits"] = str(g.get("hits", 0))
                        base["hr"] = str(g.get("hr", 0))
                        base["rbi"] = str(g.get("rbi", 0))
                        base["bb"] = str(g.get("bb", 0))
                        base["so"] = str(g.get("so", 0))
                        base["sb"] = str(g.get("sb", 0))
                    writer.writerow(base)
                    populated_rows += 1

    out_f.close()
    elapsed = time.time() - t0
    log.info(
        f"populated_rows={populated_rows} no_log_players={no_log_players} "
        f"unresolved_id={skipped_no_id} skipped_already={skipped_already} "
        f"elapsed={elapsed:.1f}s"
    )

    # Coverage check: how many in_rows got at least one prior-game row written.
    # Re-read to count distinct (slate_date, mlb_id) pairs.
    final_pairs = _read_existing_pairs(GAME_LOGS_CSV)
    log.info(f"Final unique (slate_date, mlb_id) pairs in output: {len(final_pairs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
