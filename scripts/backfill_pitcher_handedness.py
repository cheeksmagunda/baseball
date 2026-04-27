"""Backfill pitcher handedness in /data/historical_slate_results.json.

Audit (Apr 27 2026) showed `home_starter_hand` and `away_starter_hand` were 100%
null across all 421 historical game objects, despite the live pipeline relying
on handedness for batter platoon advantage (Group B B1).  The original
backfill (`scripts/backfill_slate_env_conditions.py`) tried to source
handedness from the schedule endpoint's `probablePitcher` hydration, which is
unreliable for past games.

This script reads each game's `home_starter_id` / `away_starter_id` and fetches
`pitchHand.code` from the more reliable MLB Stats API people endpoint
(`/api/v1/people/{id}`), then writes the result back.

Calibration-only: this touches /data/ historical files, never the live pipeline
DB.  Run once after each new ingest if the env-conditions backfill is skipped.

Usage:
    python scripts/backfill_pitcher_handedness.py
    python scripts/backfill_pitcher_handedness.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SLATE_RESULTS = ROOT / "data" / "historical_slate_results.json"
MLB_API = "https://statsapi.mlb.com/api/v1"
HTTP_TIMEOUT = 20

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


def fetch_hand(mlb_id: int | None) -> str | None:
    """Return 'L' or 'R' from MLB people endpoint, or None if unknown."""
    if not mlb_id:
        return None
    try:
        r = requests.get(f"{MLB_API}/people/{mlb_id}", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        people = r.json().get("people", [])
        if not people:
            return None
        return (people[0].get("pitchHand") or {}).get("code")
    except Exception as e:
        log.warning(f"Failed to fetch handedness for mlb_id={mlb_id}: {e}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report only — don't write changes")
    args = parser.parse_args()

    if not SLATE_RESULTS.exists():
        log.error(f"{SLATE_RESULTS} does not exist")
        return 1

    with SLATE_RESULTS.open() as f:
        slates = json.load(f)

    # Collect every unique pitcher ID that needs a handedness lookup
    pitcher_ids: set[int] = set()
    for slate in slates:
        for game in slate.get("games", []):
            for side in ("home", "away"):
                pid = game.get(f"{side}_starter_id")
                hand = game.get(f"{side}_starter_hand")
                if pid and not hand:
                    pitcher_ids.add(pid)

    log.info(f"Found {len(pitcher_ids)} unique pitchers needing handedness")
    if not pitcher_ids:
        log.info("Nothing to backfill — all handedness already populated.")
        return 0

    # Parallel fetch
    hand_by_id: dict[int, str | None] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_hand, pid): pid for pid in pitcher_ids}
        for fut in as_completed(futures):
            pid = futures[fut]
            hand_by_id[pid] = fut.result()
            time.sleep(0.05)  # gentle rate limit

    populated = sum(1 for h in hand_by_id.values() if h)
    log.info(f"Fetched handedness for {populated}/{len(pitcher_ids)} pitchers")

    # Apply to slates
    updates = 0
    for slate in slates:
        for game in slate.get("games", []):
            for side in ("home", "away"):
                pid = game.get(f"{side}_starter_id")
                if pid and not game.get(f"{side}_starter_hand"):
                    hand = hand_by_id.get(pid)
                    if hand:
                        game[f"{side}_starter_hand"] = hand
                        updates += 1

    log.info(f"Populated {updates} handedness fields across slates")

    if args.dry_run:
        log.info("DRY RUN — not writing changes")
        return 0

    with SLATE_RESULTS.open("w") as f:
        json.dump(slates, f, indent=2)
    log.info(f"Wrote {SLATE_RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
