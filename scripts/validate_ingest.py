"""
Pre-reseed ingest validation gate.

Checks all four data files for a given slate date and exits with:
  0 — all checks passed, safe to reseed
  1 — warnings only (unusual values) — review before proceeding
  2 — errors (duplicates, formula failures, structural issues) — fix and rerun

Usage:
  python scripts/validate_ingest.py --date 2026-04-17
"""

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"

HISTORICAL_PLAYERS = DATA / "historical_players.csv"
WINNING_DRAFTS = DATA / "historical_winning_drafts.csv"
SLATE_RESULTS = DATA / "historical_slate_results.json"
HV_STATS = DATA / "hv_player_game_stats.csv"

VALID_SLOT_MULTS = {2.0, 1.8, 1.6, 1.4, 1.2}
BASE_MULTIPLIER = 2.0

errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)
    print(f"  ERROR: {msg}")


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f"  WARN:  {msg}")


def ok(msg: str) -> None:
    print(f"  OK:    {msg}")


# ---------------------------------------------------------------------------
# historical_players.csv
# ---------------------------------------------------------------------------

def check_players(target_date: str) -> list[dict]:
    rows = []
    seen_keys: set[tuple] = set()

    with open(HISTORICAL_PLAYERS, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["date"] != target_date:
                continue
            rows.append(row)

    if not rows:
        err(f"historical_players.csv: no rows for {target_date}")
        return rows

    ok(f"historical_players.csv: {len(rows)} rows for {target_date}")

    for row in rows:
        key = (row["player_name"], row["team"])
        if key in seen_keys:
            err(f"Duplicate (player_name, team) in historical_players.csv: {key}")
        seen_keys.add(key)

        # total_value formula check
        rs_raw = row.get("real_score", "").strip()
        boost_raw = row.get("card_boost", "").strip()
        tv_raw = row.get("total_value", "").strip()

        if rs_raw and boost_raw and tv_raw:
            try:
                rs = float(rs_raw)
                boost = float(boost_raw) if boost_raw not in ("", "—") else 0.0
                tv = float(tv_raw)
                expected = rs * (BASE_MULTIPLIER + boost)
                if abs(expected - tv) > 0.02:
                    err(
                        f"{row['player_name']} ({row['team']}): total_value mismatch "
                        f"— got {tv}, expected {expected:.2f} "
                        f"(rs={rs}, boost={boost})"
                    )
            except ValueError:
                warn(f"{row['player_name']}: non-numeric rs/boost/tv — skipping formula check")

        # real_score range check
        if rs_raw:
            try:
                rs = float(rs_raw)
                if rs < -10 or rs > 20:
                    warn(f"{row['player_name']} ({row['team']}): unusual real_score={rs}")
            except ValueError:
                pass

    if len(rows) < 20:
        warn(f"historical_players.csv: only {len(rows)} unique players (minimum is 20)")

    ok(f"historical_players.csv: {len(seen_keys)} unique (player, team) pairs — no duplicates" if len(seen_keys) == len(rows) else "")
    return rows


# ---------------------------------------------------------------------------
# historical_winning_drafts.csv
# ---------------------------------------------------------------------------

def check_winning_drafts(target_date: str) -> None:
    rows = []

    with open(WINNING_DRAFTS, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["date"] != target_date:
                continue
            rows.append(row)

    if not rows:
        err(f"historical_winning_drafts.csv: no rows for {target_date}")
        return

    ok(f"historical_winning_drafts.csv: {len(rows)} rows for {target_date}")

    if len(rows) % 5 != 0:
        err(
            f"historical_winning_drafts.csv: row count {len(rows)} is not a multiple of 5 "
            "(each lineup must have exactly 5 slots)"
        )

    # Group by rank and validate each lineup
    lineups: dict[str, list[dict]] = {}
    for row in rows:
        rank = row.get("winner_rank", "")
        lineups.setdefault(rank, []).append(row)

    if len(lineups) < 4:
        warn(f"historical_winning_drafts.csv: only {len(lineups)} lineups captured (target is 20+)")

    for rank, lineup_rows in lineups.items():
        if len(lineup_rows) != 5:
            err(f"Rank {rank}: has {len(lineup_rows)} slots (expected 5)")

        slot_mults_seen = set()
        pitcher_count = 0
        for row in lineup_rows:
            try:
                sm = float(row.get("slot_mult", ""))
                if sm not in VALID_SLOT_MULTS:
                    err(f"Rank {rank}: invalid slot_mult={sm}")
                if sm in slot_mults_seen:
                    err(f"Rank {rank}: duplicate slot_mult={sm}")
                slot_mults_seen.add(sm)
            except ValueError:
                err(f"Rank {rank}: non-numeric slot_mult={row.get('slot_mult')!r}")

            pos = row.get("position", "").upper()
            if pos in ("P", "SP"):
                pitcher_count += 1

        if pitcher_count != 1:
            warn(f"Rank {rank}: expected 1 pitcher, found {pitcher_count} (may use 'P' position code)")


# ---------------------------------------------------------------------------
# historical_slate_results.json
# ---------------------------------------------------------------------------

def check_slate_results(target_date: str) -> bool:
    with open(SLATE_RESULTS, encoding="utf-8") as f:
        data = json.load(f)

    entry = next((e for e in data if e.get("date") == target_date), None)
    if entry is None:
        err(f"historical_slate_results.json: no entry for {target_date}")
        return False

    ok(f"historical_slate_results.json: entry found for {target_date}")

    games = entry.get("games", [])
    game_count = entry.get("game_count", 0)
    if len(games) != game_count:
        warn(
            f"historical_slate_results.json: game_count={game_count} "
            f"but games array has {len(games)} entries"
        )

    if game_count < 1:
        err(f"historical_slate_results.json: game_count={game_count} for {target_date}")

    return True


# ---------------------------------------------------------------------------
# hv_player_game_stats.csv
# ---------------------------------------------------------------------------

def check_hv_stats(target_date: str) -> None:
    rows = []

    with open(HV_STATS, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["date"] != target_date:
                continue
            rows.append(row)

    if not rows:
        warn(f"hv_player_game_stats.csv: no rows for {target_date} (HV stats optional but expected)")
        return

    ok(f"hv_player_game_stats.csv: {len(rows)} rows for {target_date}")

    if len(rows) < 10:
        warn(f"hv_player_game_stats.csv: only {len(rows)} rows (minimum is 10)")


# ---------------------------------------------------------------------------
# Cross-file lockstep check
# ---------------------------------------------------------------------------

def check_lockstep(target_date: str) -> None:
    files_with_date = []

    with open(HISTORICAL_PLAYERS, newline="", encoding="utf-8") as f:
        if any(r["date"] == target_date for r in csv.DictReader(f)):
            files_with_date.append("historical_players.csv")

    with open(WINNING_DRAFTS, newline="", encoding="utf-8") as f:
        if any(r["date"] == target_date for r in csv.DictReader(f)):
            files_with_date.append("historical_winning_drafts.csv")

    with open(SLATE_RESULTS, encoding="utf-8") as f:
        if any(e.get("date") == target_date for e in json.load(f)):
            files_with_date.append("historical_slate_results.json")

    with open(HV_STATS, newline="", encoding="utf-8") as f:
        if any(r["date"] == target_date for r in csv.DictReader(f)):
            files_with_date.append("hv_player_game_stats.csv")

    if len(files_with_date) < 3:
        err(
            f"Lockstep violation: date {target_date} only present in "
            f"{files_with_date} (must be in at least historical_players, "
            "historical_winning_drafts, and historical_slate_results)"
        )
    else:
        ok(f"Lockstep: {target_date} present in {files_with_date}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a slate ingest before reseeding")
    parser.add_argument("--date", required=True, help="Slate date in YYYY-MM-DD format")
    args = parser.parse_args()

    try:
        date.fromisoformat(args.date)
    except ValueError:
        print(f"Invalid date format: {args.date!r} (expected YYYY-MM-DD)")
        sys.exit(2)

    target_date = args.date
    print(f"\nValidating ingest for {target_date}\n{'=' * 45}")

    print("\n[1] historical_players.csv")
    check_players(target_date)

    print("\n[2] historical_winning_drafts.csv")
    check_winning_drafts(target_date)

    print("\n[3] historical_slate_results.json")
    check_slate_results(target_date)

    print("\n[4] hv_player_game_stats.csv")
    check_hv_stats(target_date)

    print("\n[5] Lockstep check")
    check_lockstep(target_date)

    print(f"\n{'=' * 45}")
    if errors:
        print(f"RESULT: {len(errors)} error(s), {len(warnings)} warning(s) — fix before reseeding")
        sys.exit(2)
    elif warnings:
        print(f"RESULT: 0 errors, {len(warnings)} warning(s) — review before reseeding")
        sys.exit(1)
    else:
        print("RESULT: all checks passed — safe to reseed")
        sys.exit(0)


if __name__ == "__main__":
    main()
