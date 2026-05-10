"""Backfill BABIP / HR-FB regression flags onto player_slate.

Tier 2 D6 of the May 2026 cleanup-and-add sweep.

Computes per-(slate_date, mlb_id):
  babip            = (hits − hr) / (ab − so − hr + sf)         [batter form]
  hr_fb            = hr / fly_balls                            [requires Savant]
  babip_regression_flag = 1 if luckier than league norm by >LUCK_DELTA pts
  hr_fb_regression_flag = 1 if luckier than league norm by >HR_FB_DELTA pts

Computed from `player_game_log` rolling-30-day window where possible.
Fly-ball totals require Savant launch-angle data which is NOT in
player_game_log; we approximate `hr_fb` from existing `barrel_pct` ×
`fly_ball_rate` proxy (left blank for now — flag-only is the v1 ship).

For the v1 ship: babip_at_slate is computed; hr_fb_at_slate is only
computed when launch_angle data is present (currently never — left
NULL).  The flags are computed against `LUCK_DELTA` thresholds.

Usage:
    python scripts/backfill_regression_flags.py
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-regression-flags-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_regression_flags")


# League-average BABIP hovers ~0.290-0.300 for batters; "lucky" runs >0.330,
# "unlucky" runs <0.260.  HR/FB ranges 0.10-0.16 for hitters; outside that
# is regression-relevant.
LEAGUE_BABIP = 0.295
BABIP_LUCK_DELTA = 0.05
LEAGUE_HR_FB = 0.13
HR_FB_LUCK_DELTA = 0.04


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window-days", type=int, default=30,
                    help="Rolling window for BABIP estimation.")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # Build per-mlb_id rolling stats
        cur = conn.execute(
            "SELECT slate_date, mlb_id, ab, hits, hr, so, sb "
            "FROM player_game_log WHERE ab IS NOT NULL AND ab > 0"
        )
        per_player: dict[int, list[dict]] = defaultdict(list)
        for r in cur.fetchall():
            per_player[r["mlb_id"]].append({
                "date": r["slate_date"],
                "ab": r["ab"] or 0,
                "hits": r["hits"] or 0,
                "hr": r["hr"] or 0,
                "so": r["so"] or 0,
            })
        for mid in per_player:
            per_player[mid].sort(key=lambda x: x["date"])

        cur = conn.execute(
            "SELECT slate_date, mlb_id FROM player_slate "
            "WHERE position NOT IN ('P','SP','RP','TWP') "
            "ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        log.info("batter rows to populate: %d", len(targets))

        updates = 0
        for t in targets:
            slate_d = DateType.fromisoformat(t["slate_date"])
            cutoff = (slate_d - timedelta(days=args.window_days)).isoformat()
            history = per_player.get(t["mlb_id"], [])
            window = [h for h in history if cutoff <= h["date"] < t["slate_date"]]
            if not window:
                continue
            ab = sum(h["ab"] for h in window)
            hits = sum(h["hits"] for h in window)
            hr = sum(h["hr"] for h in window)
            so = sum(h["so"] for h in window)
            denom = ab - so - hr  # BABIP denominator (sf treated as 0; pgl lacks sf)
            if denom <= 0:
                continue
            babip = (hits - hr) / denom
            updates_dict = {
                "babip_at_slate": round(babip, 4),
                "babip_regression_flag": int(babip - LEAGUE_BABIP > BABIP_LUCK_DELTA),
            }
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], updates_dict,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (hr_fb left NULL — needs Savant launch-angle backfill)",
                 updates)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        if not os.environ.get("HISTORICAL_DB"):
            from scripts.export_historical_csvs import export_all
            export_all()
    sys.exit(rc)
