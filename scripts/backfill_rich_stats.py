"""
Backfill rich platform stats onto three historical data files.

Adds to data/historical_players.csv:
  draft_count       — exact integer count (was fuzzy "2.7k" parse)
  avg_draft_slot    — average slot position (1.0–5.0) the crowd used
  most_common_slot  — most-common slot ('1'–'5')
  avg_draft_mult    — average total multiplier (slot + boost) across all drafters
  avg_draft_tv      — average TV score per drafter
  highest_draft_tv  — highest TV achieved by any single drafter
  injury_status     — "Active" / "Day-to-Day" / "Out" at scrape time

Adds to data/historical_winning_drafts.csv:
  card_boost        — boost the winner actually applied to this slot
  total_mult        — slot_mult + card_boost (= effective multiplier)

Adds to data/historical_slate_results.json (per-slate envelope):
  num_brawlers      — total unique participants on the slate

Source: same web.realapp.com direct API used by backfill_card_boost_and_drafts.py.
Auth: fresh real-request-token captured via playwright once at startup.

Usage:
    .venv-scraper/bin/python scripts/backfill_rich_stats.py
        # processes every date in the CSV (2026-03-25 → 2026-05-07)
    .venv-scraper/bin/python scripts/backfill_rich_stats.py --date 2026-04-28
        # single date
    .venv-scraper/bin/python scripts/backfill_rich_stats.py --force
        # overwrite already-populated dates
"""
import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# scraper/storage_state.json lives in the main repo checkout, not worktrees.
# Walk up from ROOT to find it.
def _find_storage_state() -> Path:
    candidate = ROOT / "scraper" / "storage_state.json"
    if candidate.exists():
        return candidate
    # Worktree: ROOT is .claude/worktrees/<name>; main repo is three levels up.
    for parent in ROOT.parents:
        c = parent / "scraper" / "storage_state.json"
        if c.exists():
            return c
    return candidate  # return non-existent path so the caller emits a clear error

STORAGE_STATE = _find_storage_state()

PLAYERS_CSV = DATA_DIR / "historical_players.csv"
DRAFTS_CSV = DATA_DIR / "historical_winning_drafts.csv"
RESULTS_JSON = DATA_DIR / "historical_slate_results.json"

API_BASE = "https://web.realapp.com"

LEADERBOARD_SECTIONS = {
    "highestBoostedValuePlayers",
    "popularPlayers",
    "mostCommon3xPlayers",
}

NEW_PLAYER_COLS = (
    "draft_count",
    "avg_draft_slot",
    "most_common_slot",
    "avg_draft_mult",
    "avg_draft_tv",
    "highest_draft_tv",
    "injury_status",
)
NEW_DRAFT_COLS = ("card_boost", "total_mult")

sys.path.insert(0, str(ROOT))
from scripts.scrape_realsports_daily import (  # noqa: E402
    _fetch_mlb_player_info,
    _name_for,
    _name_normalize,
    _team_key_normalize,
)
from scripts.backfill_card_boost_and_drafts import (  # noqa: E402
    _capture_live_headers,
    _http_get,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_rich_stats")


# ---------------------------------------------------------------------------
# API fetchers
# ---------------------------------------------------------------------------

def fetch_contest_id(target_date: str, headers: dict) -> int:
    url = f"{API_BASE}/home/mlb/day/next?cohort=0&day={target_date}"
    daily = _http_get(url, headers)
    contests = (
        daily.get("content", {})
        .get("config", {})
        .get("dailyDraftInfo", {})
        .get("contests", [])
    )
    if not contests:
        raise RuntimeError(f"no contests in daily payload for {target_date}")
    return contests[0]["id"]


def fetch_stats(contest_id: int, headers: dict) -> dict:
    url = f"{API_BASE}/games/playerratingcontest/{contest_id}/stats"
    return _http_get(url, headers)


def fetch_entries(contest_id: int, headers: dict) -> list[dict]:
    # Query params are required: without them the platform returns the wrong sport's contest
    url = f"{API_BASE}/games/playerratingcontest/{contest_id}/entries?contestType=sport&isGuillotine=false"
    data = _http_get(url, headers)
    return data.get("entries", [])


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def build_player_lookup(stats: dict, player_info: dict) -> dict:
    """
    Returns {(name, team): stats_dict} keyed by both MLB full name and
    platform display name so CSV rows match regardless of which form was stored.

    When a player appears in multiple leaderboard sections, keeps the entry
    from the section with the highest count (most representative sample —
    almost always the popularPlayers section).
    """
    by_key: dict[tuple[str, str], dict] = {}

    for sec in stats.get("draftStats", []):
        if sec.get("sectionName") not in LEADERBOARD_SECTIONS:
            continue
        for p in sec["players"]:
            pl = p.get("player") or {}
            tm = p.get("team") or {}
            if not tm.get("key"):
                continue

            pid = pl.get("id") or p.get("playerId")
            team = _team_key_normalize(tm["key"])
            full_name = _name_for(pid, player_info, pl.get("displayName", ""))
            display_name = _name_normalize(pl.get("displayName") or "")

            count = p.get("count") or 0
            entry = {
                "draft_count": count,
                "avg_draft_slot": _safe_round(p.get("avgPosition"), 3),
                "most_common_slot": p.get("mostCommonPosition", ""),
                "avg_draft_mult": _safe_round(p.get("avgMultiplier"), 4),
                "avg_draft_tv": _safe_round(p.get("avgScore"), 4),
                "highest_draft_tv": _safe_round(p.get("highestScore"), 4),
                "injury_status": pl.get("injuryStatus", ""),
            }

            for key in {(full_name, team), (display_name, team)}:
                existing = by_key.get(key)
                if existing is None or count > (existing.get("draft_count") or 0):
                    by_key[key] = entry

    return by_key


def build_draft_boost_lookup(entries: list[dict]) -> dict:
    """
    Returns {(winner_rank, slot_index): {card_boost, total_mult}}.
    slot_index = player["order"] + 1 (1-indexed, matching the CSV).
    """
    lookup: dict[tuple[int, int], dict] = {}
    for entry in entries:
        rank = entry.get("rank")
        if rank is None:
            continue
        lineup = entry.get("additionalInfo", {}).get("lineup", [])
        for player in lineup:
            order = player.get("order")
            if order is None:
                continue
            slot_index = order + 1
            boost = player.get("multiplierBonus")
            mult = player.get("multiplier")  # total = slot_mult + card_boost
            # Only store if at least one field present
            if boost is None and mult is None:
                continue
            lookup[(rank, slot_index)] = {
                "card_boost": boost,
                "total_mult": mult,
            }
    return lookup


def _safe_round(v, dp: int):
    if v is None:
        return ""
    try:
        return round(float(v), dp)
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# CSV / JSON I/O
# ---------------------------------------------------------------------------

def load_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def date_has_draft_count(rows: list[dict], target_date: str) -> bool:
    date_rows = [r for r in rows if r.get("date") == target_date]
    if not date_rows:
        return False
    return all(r.get("draft_count") not in (None, "") for r in date_rows)


# ---------------------------------------------------------------------------
# Per-date backfill
# ---------------------------------------------------------------------------

def backfill_one_date(
    target_date: str,
    player_rows: list[dict],
    draft_rows: list[dict],
    results: list[dict],
    headers: dict,
) -> tuple[int, int, int, int]:
    """
    Returns (player_matched, player_missed, draft_matched, draft_missed).
    Mutates player_rows, draft_rows, results in place.
    """

    # 1. Daily payload → contest_id
    log.info("  [1/3] daily payload → contestId")
    contest_id = fetch_contest_id(target_date, headers)
    log.info(f"    contestId={contest_id}")
    time.sleep(0.6)

    # 2. Stats payload → per-player fields + num_brawlers
    log.info("  [2/3] stats payload")
    stats = fetch_stats(contest_id, headers)
    num_brawlers = stats.get("contest", {}).get("numBrawlers") or 0
    log.info(f"    numBrawlers={num_brawlers}")
    time.sleep(0.6)

    # 3. Entries payload → per-slot card_boost
    log.info("  [3/3] entries payload")
    entries = fetch_entries(contest_id, headers)
    log.info(f"    {len(entries)} entries")
    time.sleep(0.6)

    # --- Update historical_slate_results.json ---
    for entry in results:
        if entry.get("date") == target_date:
            entry["num_brawlers"] = num_brawlers

    # --- Resolve player names via MLB API ---
    player_ids: set[int] = set()
    for sec in stats.get("draftStats", []):
        if sec.get("sectionName") in LEADERBOARD_SECTIONS:
            for p in sec["players"]:
                pid = p.get("player", {}).get("id") or p.get("playerId")
                if pid:
                    player_ids.add(int(pid))
    player_info = _fetch_mlb_player_info(list(player_ids))

    # --- Build lookups ---
    stats_lookup = build_player_lookup(stats, player_info)
    boost_lookup = build_draft_boost_lookup(entries)

    # Name-only fallback index (handles team mismatches from mid-season trades)
    name_only: dict[str, dict] = {}
    name_collisions: set[str] = set()
    for (name, _team), val in stats_lookup.items():
        if name in name_only and name_only[name] != val:
            name_collisions.add(name)
        else:
            name_only[name] = val

    # --- Patch historical_players.csv ---
    # Clear first so a partial prior run doesn't leave stale values
    for row in player_rows:
        if row.get("date") == target_date:
            for col in NEW_PLAYER_COLS:
                row[col] = ""

    player_matched = player_missed = 0
    missed_examples: list[str] = []

    for row in player_rows:
        if row.get("date") != target_date:
            continue
        norm_name = _name_normalize(row["player_name"])
        key = (norm_name, row["team"])

        val = stats_lookup.get(key)
        if val is None and norm_name not in name_collisions:
            val = name_only.get(norm_name)

        if val:
            for col in NEW_PLAYER_COLS:
                row[col] = val.get(col, "")
            player_matched += 1
        else:
            player_missed += 1
            if len(missed_examples) < 5:
                missed_examples.append(f"{row['player_name']} ({row['team']})")

    log.info(f"    players matched={player_matched} missed={player_missed}"
             + (f" e.g. {missed_examples}" if missed_examples else ""))

    # --- Patch historical_winning_drafts.csv ---
    for row in draft_rows:
        if row.get("date") == target_date:
            for col in NEW_DRAFT_COLS:
                row[col] = ""

    draft_matched = draft_missed = 0

    for row in draft_rows:
        if row.get("date") != target_date:
            continue
        try:
            rank = int(row["winner_rank"])
            slot = int(row["slot_index"])
        except (KeyError, ValueError):
            draft_missed += 1
            continue
        val = boost_lookup.get((rank, slot))
        if val:
            for col in NEW_DRAFT_COLS:
                row[col] = val.get(col, "")
            draft_matched += 1
        else:
            draft_missed += 1

    log.info(f"    draft slots matched={draft_matched} missed={draft_missed}")

    return player_matched, player_missed, draft_matched, draft_missed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD; default=all dates missing draft_count")
    ap.add_argument("--force", action="store_true",
                    help="re-fetch even if already populated")
    args = ap.parse_args()

    if not STORAGE_STATE.exists():
        sys.exit(f"ERROR: {STORAGE_STATE} not found — run scraper --refresh-auth first")

    log.info("Capturing fresh auth headers via playwright ...")
    headers = _capture_live_headers()

    # Load all three data structures
    player_fieldnames, player_rows = load_csv(PLAYERS_CSV)
    draft_fieldnames, draft_rows = load_csv(DRAFTS_CSV)
    results = json.loads(RESULTS_JSON.read_text())

    # Extend fieldnames with new columns (existing rows get blank values)
    for col in NEW_PLAYER_COLS:
        if col not in player_fieldnames:
            player_fieldnames.append(col)
            for row in player_rows:
                row.setdefault(col, "")

    for col in NEW_DRAFT_COLS:
        if col not in draft_fieldnames:
            draft_fieldnames.append(col)
            for row in draft_rows:
                row.setdefault(col, "")

    all_dates = sorted({r["date"] for r in player_rows})

    if args.date:
        if args.date not in all_dates:
            sys.exit(f"ERROR: {args.date} not found in {PLAYERS_CSV.name}")
        target_dates = [args.date]
    elif args.force:
        target_dates = all_dates
    else:
        target_dates = [
            d for d in all_dates
            if not date_has_draft_count(player_rows, d)
        ]

    log.info(f"Will process {len(target_dates)} / {len(all_dates)} dates")

    if not target_dates:
        log.info("Nothing to do (all dates already have draft_count). Use --force to re-fetch.")
        return

    totals = dict(pm=0, pm2=0, dm=0, dm2=0)
    failed: list[str] = []

    for i, d in enumerate(target_dates, 1):
        log.info(f"\n[{i}/{len(target_dates)}] {d}")
        try:
            pm, pm2, dm, dm2 = backfill_one_date(
                d, player_rows, draft_rows, results, headers
            )
            totals["pm"] += pm
            totals["pm2"] += pm2
            totals["dm"] += dm
            totals["dm2"] += dm2

            # Persist after every date so a crash mid-run doesn't lose progress
            write_csv(PLAYERS_CSV, player_fieldnames, player_rows)
            write_csv(DRAFTS_CSV, draft_fieldnames, draft_rows)
            RESULTS_JSON.write_text(json.dumps(results, indent=2))
            log.info(f"  wrote all three files for {d}")

        except Exception as e:
            log.error(f"  FAILED on {d}: {e!r}")
            failed.append(d)
            continue

    log.info("\n=== Summary ===")
    log.info(f"Dates attempted: {len(target_dates) - len(failed)} / {len(target_dates)}")
    log.info(f"Players: matched={totals['pm']} missed={totals['pm2']}")
    log.info(f"Draft slots: matched={totals['dm']} missed={totals['dm2']}")
    if failed:
        log.info(f"Failed dates ({len(failed)}): {failed}")
        sys.exit(1)


if __name__ == "__main__":
    main()
    # Step 3 hook: re-ingest CSVs into data/historical.db so downstream
    # readers (Step 4) see the new values.  Cheap (~1s) and idempotent.
    import sys as _sys
    from pathlib import Path as _Path
    _repo = _Path(__file__).resolve().parents[1]
    if str(_repo) not in _sys.path:
        _sys.path.insert(0, str(_repo))
    from app.core import historical_db
    historical_db.rebuild_from_csvs_and_export()
