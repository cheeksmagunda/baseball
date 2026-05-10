"""Backfill historical_slate_results.json, hv_player_game_stats.csv, and
historical_app_picks.csv for dates where box scores have not yet been filled.

Scope is auto-detected:
  - Slate-result dates whose ``games`` array is empty are refilled from the
    MLB Stats API schedule endpoint (Final games only).
  - Highest-Value leaderboard rows (``is_highest_value=1`` in
    historical_players.csv) whose matching hv_player_game_stats.csv row is
    missing OR is a placeholder (empty batting + pitching stats) are filled
    from the MLB Stats API boxscore.
  - App picks rows in historical_app_picks.csv whose box-stat columns are
    blank (written by the post-lock monitor at slate completion) are filled
    from the MLB Stats API boxscore.  real_score is left untouched — fill it
    manually after the platform posts results.

No fallbacks: if the MLB API does not return a Final game, or a player
does not appear in the corresponding team's boxscore, the row is logged and
skipped. Never guess, never substitute.

Historical data remains reference-only — this script only touches files in
/data/ and does not modify the live pipeline or DB.

Usage:
    python scripts/backfill_slate_results_and_hv_stats.py
"""

import asyncio
import csv
import json
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.constants import canonicalize_team
from app.core.mlb_api import _get, get_game_boxscore

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SLATE_RESULTS = DATA_DIR / "historical_slate_results.json"
HV_STATS = DATA_DIR / "hv_player_game_stats.csv"
HISTORICAL_PLAYERS = DATA_DIR / "historical_players.csv"
APP_PICKS = DATA_DIR / "historical_app_picks.csv"

APP_PICK_FIELDNAMES = [
    "date", "slot_index", "player_name", "team", "position",
    "slot_mult", "filter_ev", "env_score", "total_score", "real_score",
    "ab", "r", "h", "hr", "rbi", "bb", "so",
    "ip", "er", "k_pitching", "decision",
]

HV_FIELDNAMES = [
    "date", "player_name", "team_actual", "position",
    "real_score", "game_result",
    "ab", "r", "h", "hr", "rbi", "bb", "so",
    "ip", "er", "k_pitching", "decision", "notes",
]


def _normalize_name(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_name.lower().split())


# ---------------------------------------------------------------------------
# Slate-results backfill
# ---------------------------------------------------------------------------

async def fetch_final_games(game_date: str) -> list[dict]:
    """Return list of Final games for the date, each carrying gamePk for later
    boxscore lookup. Team abbreviations are left raw (matches existing
    historical_slate_results.json convention: "AZ", "ATH")."""
    data = await _get("/schedule", {
        "date": game_date,
        "sportId": 1,
        "hydrate": "team,linescore",
    })
    games: list[dict] = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            state = g.get("status", {}).get("abstractGameState")
            if state != "Final":
                continue
            teams = g.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            home_abbr = home.get("team", {}).get("abbreviation")
            away_abbr = away.get("team", {}).get("abbreviation")
            home_score = home.get("score")
            away_score = away.get("score")
            if not (home_abbr and away_abbr) or home_score is None or away_score is None:
                continue
            # winner/loser/winner_score/loser_score are derived on export
            # from home/away/home_score/away_score — not stored.
            games.append({
                "game_pk": g.get("gamePk"),
                "home": home_abbr,
                "away": away_abbr,
                "home_score": home_score,
                "away_score": away_score,
            })
    return games


def _game_dict_no_pk(g: dict) -> dict:
    return {k: v for k, v in g.items() if k != "game_pk"}


def load_slate_results() -> list[dict]:
    with SLATE_RESULTS.open() as f:
        return json.load(f)


def save_slate_results(data: list[dict]) -> None:
    with SLATE_RESULTS.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _dates_needing_game_backfill(slate_data: list[dict]) -> list[str]:
    return [e["date"] for e in slate_data if not e.get("games")]


# ---------------------------------------------------------------------------
# HV box-stat backfill
# ---------------------------------------------------------------------------

def load_hv_leaderboard_players() -> list[dict]:
    """Return HV (is_highest_value=1) rows from historical_players.csv as a
    list of dicts with the fields needed to construct an hv_player_game_stats
    row."""
    out: list[dict] = []
    with HISTORICAL_PLAYERS.open() as f:
        for row in csv.DictReader(f):
            if row.get("is_highest_value") == "1":
                out.append({
                    "date": row["date"],
                    "player_name": row["player_name"],
                    "team": row["team"],
                    "position": row["position"],
                    "real_score": row["real_score"],
                })
    return out


def load_hv_stats() -> list[dict]:
    """Read the HV stats CSV. Some legacy placeholder rows have an extra comma
    before the notes column (20 fields for a 19-field header), which surfaces
    as a ``None`` key in DictReader. Consolidate that stray value into the
    notes column (if empty) so the row round-trips cleanly through the writer.
    """
    rows: list[dict] = []
    with HV_STATS.open() as f:
        for row in csv.DictReader(f):
            extras = row.pop(None, None)
            if extras and not row.get("notes"):
                row["notes"] = " ".join(str(e) for e in extras if e).strip()
            rows.append(row)
    return rows


def _hv_row_is_placeholder(row: dict) -> bool:
    bat_empty = all(row.get(k, "") == "" for k in ("ab", "r", "h", "hr", "rbi", "bb", "so"))
    pit_empty = all(row.get(k, "") == "" for k in ("ip", "er", "k_pitching", "decision"))
    if bat_empty and pit_empty:
        return True
    # Partial batter row: has some hitting stats but ab is missing — needs full boxscore.
    if row.get("ab", "") == "" and any(row.get(k, "") != "" for k in ("h", "hr", "rbi", "r")):
        return True
    # Partial pitcher row: has some pitching stats but ip is missing.
    if row.get("ip", "") == "" and any(row.get(k, "") != "" for k in ("er", "k_pitching", "decision")):
        return True
    return False


def _find_player_in_boxscore(box: dict, player_name: str, team_abbr: str) -> dict | None:
    """Locate the player's team side in the boxscore, then match by normalized
    full name. No cross-team fallback — returns None if no exact match."""
    target_norm = _normalize_name(player_name)
    team_canon = canonicalize_team(team_abbr)
    for side in ("home", "away"):
        team = box.get("teams", {}).get(side, {})
        box_abbr = team.get("team", {}).get("abbreviation", "")
        if canonicalize_team(box_abbr) != team_canon:
            continue
        for p in team.get("players", {}).values():
            full_name = p.get("person", {}).get("fullName", "")
            if _normalize_name(full_name) == target_norm:
                return p
    return None


def _pitcher_decision(pitching: dict) -> str:
    if pitching.get("wins", 0) > 0:
        return "W"
    if pitching.get("losses", 0) > 0:
        return "L"
    if pitching.get("saves", 0) > 0:
        return "SV"
    if pitching.get("holds", 0) > 0:
        return "HOLD"
    return "ND"


def _game_result_str(game: dict) -> str:
    return f"{game['away']} {game['away_score']} {game['home']} {game['home_score']}"


def _vs_notation(player_team: str, game: dict) -> str:
    team_canon = canonicalize_team(player_team)
    if canonicalize_team(game["home"]) == team_canon:
        return f"vs {game['away']} (home)"
    return f"vs {game['home']} (away)"


def _build_hv_row(hv: dict, game: dict, box_player: dict) -> dict | None:
    stats = box_player.get("stats", {}) or {}
    bat = stats.get("batting", {}) or {}
    pit = stats.get("pitching", {}) or {}
    is_pitcher_slot = hv["position"].upper() == "P"

    row = {k: "" for k in HV_FIELDNAMES}
    row.update({
        "date": hv["date"],
        "player_name": hv["player_name"],
        "team_actual": hv["team"],
        "position": hv["position"],
        "real_score": hv["real_score"],
        "game_result": _game_result_str(game),
    })

    if is_pitcher_slot and pit.get("inningsPitched"):
        row["ip"] = pit.get("inningsPitched", "")
        row["er"] = pit.get("earnedRuns", "")
        row["k_pitching"] = pit.get("strikeOuts", "")
        row["decision"] = _pitcher_decision(pit)
        summary = pit.get("summary", "").strip()
        row["notes"] = f"{summary} | {_vs_notation(hv['team'], game)}" if summary else _vs_notation(hv["team"], game)
        return row

    if bat and (bat.get("atBats", 0) or bat.get("plateAppearances", 0)):
        row["ab"] = float(bat.get("atBats", 0))
        row["r"] = float(bat.get("runs", 0))
        row["h"] = float(bat.get("hits", 0))
        row["hr"] = float(bat.get("homeRuns", 0))
        row["rbi"] = float(bat.get("rbi", 0))
        row["bb"] = float(bat.get("baseOnBalls", 0))
        row["so"] = float(bat.get("strikeOuts", 0))
        summary = bat.get("summary", "").strip()
        row["notes"] = f"{summary} | {_vs_notation(hv['team'], game)}" if summary else _vs_notation(hv["team"], game)
        return row

    # DNP — no stats to record
    return None


async def backfill_hv_stats(all_games: dict[str, list[dict]]) -> None:
    if not all_games:
        print("[hv] No games available — skipping HV backfill.")
        return

    target_dates = set(all_games.keys())
    hv_players = [p for p in load_hv_leaderboard_players() if p["date"] in target_dates]

    existing_rows = load_hv_stats()

    def _key(r: dict) -> tuple[str, str]:
        return (r["date"], _normalize_name(r["player_name"]))

    existing_by_key = {_key(r): r for r in existing_rows}

    targets: list[tuple[dict, str]] = []
    for p in hv_players:
        k = (p["date"], _normalize_name(p["player_name"]))
        if k in existing_by_key:
            if _hv_row_is_placeholder(existing_by_key[k]):
                targets.append((p, "replace"))
        else:
            targets.append((p, "append"))

    if not targets:
        print("[hv] No HV rows require backfill.")
        return

    print(f"[hv] {len(targets)} HV row(s) to backfill")

    games_by_team: dict[tuple[str, str], dict] = {}
    for d, games in all_games.items():
        for g in games:
            games_by_team[(d, canonicalize_team(g["home"]))] = g
            games_by_team[(d, canonicalize_team(g["away"]))] = g

    box_cache: dict[int, dict] = {}
    resolved: list[tuple[tuple[str, str], dict, str]] = []
    for p, action in targets:
        # Prefer the corrected team_actual from the existing HV stats row over
        # the (possibly stale) team captured in historical_players.csv.
        if action == "replace":
            existing = existing_by_key.get((p["date"], _normalize_name(p["player_name"])))
            if existing and existing.get("team_actual"):
                p = {**p, "team": existing["team_actual"]}
        team_canon = canonicalize_team(p["team"])
        gk = (p["date"], team_canon)
        game = games_by_team.get(gk)
        if not game:
            print(f"[hv]   SKIP {p['date']} {p['player_name']} ({p['team']}): no game found")
            continue
        pk = game["game_pk"]
        if pk not in box_cache:
            print(f"[hv]   Fetching boxscore for {game['away']} @ {game['home']} ({pk})...")
            box_cache[pk] = await get_game_boxscore(pk)
        box_player = _find_player_in_boxscore(box_cache[pk], p["player_name"], team_canon)
        if not box_player:
            print(f"[hv]   SKIP {p['date']} {p['player_name']} ({p['team']}): not on boxscore roster")
            continue
        row = _build_hv_row(p, game, box_player)
        if row is None:
            print(f"[hv]   SKIP {p['date']} {p['player_name']}: no batting/pitching stats (DNP)")
            continue
        k = (p["date"], _normalize_name(p["player_name"]))
        resolved.append((k, row, action))

    if not resolved:
        print("[hv] All targets failed — no rows written.")
        return

    replacements = {k: row for k, row, action in resolved if action == "replace"}
    additions = [row for _, row, action in resolved if action == "append"]

    updated_rows: list[dict] = []
    for r in existing_rows:
        k = _key(r)
        if k in replacements:
            updated_rows.append(replacements[k])
        else:
            updated_rows.append(r)
    updated_rows.extend(additions)

    # Preserve any extra columns added by other backfill scripts
    # (e.g. ops_at_slate, iso_at_slate from backfill_player_season_stats_at_slate).
    extra_keys: list[str] = []
    seen = set(HV_FIELDNAMES)
    for r in updated_rows:
        for k in r.keys():
            if k is None or k in seen:
                continue
            seen.add(k)
            extra_keys.append(k)
    fieldnames = HV_FIELDNAMES + extra_keys

    with HV_STATS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(updated_rows)

    print(f"[hv] Wrote {len(replacements)} replacement + {len(additions)} new row(s) to {HV_STATS.name}")


# ---------------------------------------------------------------------------
# App picks backfill
# ---------------------------------------------------------------------------

def load_app_picks() -> list[dict]:
    if not APP_PICKS.exists():
        return []
    with APP_PICKS.open() as f:
        return list(csv.DictReader(f))


def _app_pick_needs_box_backfill(row: dict) -> bool:
    bat_cols = ("ab", "r", "h", "hr", "rbi", "bb", "so")
    pit_cols = ("ip", "er", "k_pitching", "decision")
    return (
        all(row.get(c, "") == "" for c in bat_cols)
        and all(row.get(c, "") == "" for c in pit_cols)
    )


def _build_app_pick_box_stats(pick_row: dict, box_player: dict) -> dict:
    """Return a copy of pick_row with box stats populated from the boxscore."""
    stats = box_player.get("stats", {}) or {}
    bat = stats.get("batting", {}) or {}
    pit = stats.get("pitching", {}) or {}
    is_pitcher = pick_row["position"].upper() in ("P", "SP", "RP")

    updated = dict(pick_row)
    if is_pitcher and pit.get("inningsPitched"):
        updated["ip"] = pit.get("inningsPitched", "")
        updated["er"] = pit.get("earnedRuns", "")
        updated["k_pitching"] = pit.get("strikeOuts", "")
        updated["decision"] = _pitcher_decision(pit)
    elif bat and (bat.get("atBats", 0) or bat.get("plateAppearances", 0)):
        updated["ab"] = bat.get("atBats", 0)
        updated["r"] = bat.get("runs", 0)
        updated["h"] = bat.get("hits", 0)
        updated["hr"] = bat.get("homeRuns", 0)
        updated["rbi"] = bat.get("rbi", 0)
        updated["bb"] = bat.get("baseOnBalls", 0)
        updated["so"] = bat.get("strikeOuts", 0)
    return updated


async def backfill_app_pick_stats(all_games: dict[str, list[dict]]) -> None:
    rows = load_app_picks()
    if not rows:
        print("[app] historical_app_picks.csv is empty — nothing to backfill.")
        return

    targets = [
        r for r in rows
        if r["date"] in all_games and _app_pick_needs_box_backfill(r)
    ]
    if not targets:
        print("[app] No app pick rows require box-stat backfill.")
        return

    print(f"[app] {len(targets)} app pick row(s) to backfill")

    games_by_team: dict[tuple[str, str], dict] = {}
    for d, games in all_games.items():
        for g in games:
            games_by_team[(d, canonicalize_team(g["home"]))] = g
            games_by_team[(d, canonicalize_team(g["away"]))] = g

    box_cache: dict[int, dict] = {}
    updated_by_key: dict[tuple[str, str], dict] = {}

    for pick in targets:
        d = pick["date"]
        team_canon = canonicalize_team(pick["team"])
        game = games_by_team.get((d, team_canon))
        if not game:
            print(f"[app]   SKIP {d} {pick['player_name']} ({pick['team']}): no Final game found")
            continue
        pk = game["game_pk"]
        if pk not in box_cache:
            print(f"[app]   Fetching boxscore for {game['away']} @ {game['home']} ({pk})...")
            box_cache[pk] = await get_game_boxscore(pk)
        box_player = _find_player_in_boxscore(box_cache[pk], pick["player_name"], team_canon)
        if not box_player:
            print(f"[app]   SKIP {d} {pick['player_name']} ({pick['team']}): not in boxscore")
            continue
        key = (d, _normalize_name(pick["player_name"]))
        updated_by_key[key] = _build_app_pick_box_stats(pick, box_player)
        print(f"[app]   OK   {d} {pick['player_name']}")

    if not updated_by_key:
        print("[app] All app pick targets failed — no rows written.")
        return

    updated_rows = []
    for r in rows:
        k = (r["date"], _normalize_name(r["player_name"]))
        updated_rows.append(updated_by_key.get(k, r))

    with APP_PICKS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=APP_PICK_FIELDNAMES)
        w.writeheader()
        w.writerows(updated_rows)

    print(f"[app] Updated {len(updated_by_key)} row(s) in {APP_PICKS.name}")


def _dates_needing_app_pick_backfill() -> set[str]:
    return {r["date"] for r in load_app_picks() if _app_pick_needs_box_backfill(r)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _dates_needing_hv_backfill() -> set[str]:
    """Dates where at least one HV player row is missing or a placeholder."""
    hv_players = load_hv_leaderboard_players()
    existing = {(r["date"], _normalize_name(r["player_name"])): r for r in load_hv_stats()}
    needed: set[str] = set()
    for p in hv_players:
        k = (p["date"], _normalize_name(p["player_name"]))
        row = existing.get(k)
        if row is None or _hv_row_is_placeholder(row):
            needed.add(p["date"])
    return needed


async def main() -> None:
    slate_data = load_slate_results()
    slate_needing = set(_dates_needing_game_backfill(slate_data))
    hv_needing = _dates_needing_hv_backfill()
    app_needing = _dates_needing_app_pick_backfill()
    target_dates = sorted(slate_needing | hv_needing | app_needing)

    if not target_dates:
        print("Nothing to backfill.")
        return

    print(f"Target dates: {target_dates}")
    print(f"  slate-results backfill: {sorted(slate_needing)}")
    print(f"  HV stats backfill:      {sorted(hv_needing)}")
    print(f"  app picks backfill:     {sorted(app_needing)}")

    fetched_by_date: dict[str, list[dict]] = {}
    for d in target_dates:
        print(f"[slate] Fetching schedule for {d}...")
        games = await fetch_final_games(d)
        print(f"[slate]   {len(games)} Final game(s)")
        fetched_by_date[d] = games

    if slate_needing:
        now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for entry in slate_data:
            if entry["date"] not in slate_needing:
                continue
            games = fetched_by_date[entry["date"]]
            entry["game_count"] = len(games)
            entry["games"] = [_game_dict_no_pk(g) for g in games]
            entry["previous_source"] = entry.get("source", "")
            entry["source"] = "mlb_stats_api_backfill"
            entry["saved_at"] = now_iso
        save_slate_results(slate_data)
        print(f"[slate] Updated {SLATE_RESULTS.name} ({len(slate_needing)} date(s))")

    if hv_needing:
        await backfill_hv_stats({d: fetched_by_date[d] for d in hv_needing})

    if app_needing:
        await backfill_app_pick_stats({d: fetched_by_date[d] for d in app_needing})


if __name__ == "__main__":
    asyncio.run(main())
    # Step 3 hook: re-ingest CSVs into data/historical.db so downstream
    # readers (Step 4) see the new values.  Cheap (~1s) and idempotent.
    import sys as _sys
    from pathlib import Path as _Path
    _repo = _Path(__file__).resolve().parents[1]
    if str(_repo) not in _sys.path:
        _sys.path.insert(0, str(_repo))
    from app.core import historical_db
    historical_db.rebuild_from_csvs_and_export()
