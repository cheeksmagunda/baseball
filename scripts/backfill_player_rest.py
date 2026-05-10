"""Backfill player rest features onto player_slate.

Consolidated May 2026 — replaces backfill_pitcher_rest.py.  Three
columns produced from `player_game_log`:

  pitcher_rest_days         — days between slate_date and the pitcher's
                              most recent appearance with IP > 0.  NULL
                              for non-pitchers and for pitchers with no
                              prior appearance in the corpus.
  player_consecutive_starts — count of trailing consecutive game-dates
                              (batter AB > 0 OR pitcher IP > 0) ending
                              at slate_date − 1.  Resets to 0 on any
                              calendar gap.
  player_days_since_rest    — days since the player last had a calendar
                              day with no logged game.  NULL when the
                              player has never rested in the corpus.

`pitcher_rest_days` (since-last-IP) and `player_days_since_rest`
(since-last-day-off) measure different things — both are kept for
calibration.

Usage:
    python scripts/backfill_player_rest.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date as DateType, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-player-rest-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_player_rest")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # Pitcher-only: dates with IP > 0 (for pitcher_rest_days).
        cur = conn.execute(
            "SELECT mlb_id, game_date FROM player_game_log "
            "WHERE ip IS NOT NULL AND ip > 0 "
            "ORDER BY mlb_id, game_date"
        )
        pitched: dict[int, list[str]] = defaultdict(list)
        for r in cur.fetchall():
            pitched[r["mlb_id"]].append(r["game_date"])

        # All players: dates with EITHER AB > 0 OR IP > 0 (for consecutive
        # starts + days_since_rest).
        cur = conn.execute(
            "SELECT mlb_id, game_date FROM player_game_log "
            "WHERE (ab IS NOT NULL AND ab > 0) OR (ip IS NOT NULL AND ip > 0) "
            "ORDER BY mlb_id, game_date"
        )
        played: dict[int, list[str]] = defaultdict(list)
        for r in cur.fetchall():
            played[r["mlb_id"]].append(r["game_date"])
        log.info(
            "player_game_log indexed: %d players (any), %d pitchers (IP>0)",
            len(played), len(pitched),
        )

        if args.force:
            where = "WHERE 1=1"
        else:
            where = "WHERE player_consecutive_starts IS NULL OR pitcher_rest_days IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, mlb_id, position FROM player_slate {where} "
            "ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        log.info("player rows to populate: %d", len(targets))

        PITCHER_POSITIONS = {"P", "SP", "RP", "TWP"}
        updates = 0
        for t in targets:
            slate_d = DateType.fromisoformat(t["slate_date"])
            updates_dict: dict = {}

            # pitcher_rest_days — only for pitchers, only based on IP>0 history
            if t["position"] in PITCHER_POSITIONS:
                ip_history = pitched.get(t["mlb_id"], [])
                prior_ip_dates = [
                    DateType.fromisoformat(d) for d in ip_history if d < t["slate_date"]
                ]
                if prior_ip_dates:
                    most_recent = max(prior_ip_dates)
                    updates_dict["pitcher_rest_days"] = (slate_d - most_recent).days

            history = played.get(t["mlb_id"], [])
            prior_dates = [DateType.fromisoformat(d) for d in history if d < t["slate_date"]]
            if not prior_dates:
                if updates_dict:
                    historical_db.update_player_slate_columns(
                        conn, t["slate_date"], t["mlb_id"], updates_dict,
                    )
                    updates += 1
                continue
            prior_dates.sort()

            # consecutive_starts: walk backwards from slate_date − 1, count
            # consecutive trailing dates that are 1 day apart with no gap.
            cursor = slate_d - timedelta(days=1)
            consecutive = 0
            for pd_idx in range(len(prior_dates) - 1, -1, -1):
                pd_dt = prior_dates[pd_idx]
                if pd_dt > cursor:
                    continue
                if pd_dt == cursor:
                    consecutive += 1
                    cursor -= timedelta(days=1)
                elif pd_dt < cursor:
                    break

            # days_since_rest: find the largest gap day in the player's
            # history where they DIDN'T play between two playing dates,
            # then count slate_date − last_rest_day.
            # Simpler: walk consecutive prior days; when a gap exists,
            # mark that gap's start as the last rest.
            last_rest_day: DateType | None = None
            for i in range(len(prior_dates) - 1, 0, -1):
                gap = (prior_dates[i] - prior_dates[i - 1]).days
                if gap > 1:
                    # The day(s) between are rest; closest to slate is gap-1.
                    last_rest_day = prior_dates[i] - timedelta(days=1)
                    break
            days_since = (slate_d - last_rest_day).days if last_rest_day else None

            updates_dict["player_consecutive_starts"] = consecutive
            if days_since is not None:
                updates_dict["player_days_since_rest"] = days_since
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], updates_dict,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d", updates)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0 and not os.environ.get("HISTORICAL_DB"):
        from scripts.export_historical_csvs import export_all
        export_all()
    sys.exit(rc)
