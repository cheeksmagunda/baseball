"""Backfill Win-Probability-Added (WPA) per HV player game.

Tier 3 D11 of the May 2026 cleanup-and-add sweep.

Writes to label_event(label_type='wpa') — one row per HV player per game,
label_value = WPA contribution (sum of |delta_home_win_exp| while batter
hit).  Storing as a label_event rather than a column on
hv_player_game_stats keeps the existing CSV header stable and lets WPA
land for non-HV players too if a future calibration wants it.

Source: bulk Statcast season pull (Baseball Savant via pybaseball),
shared with backfill_recent_handedness_splits and statcast_pa.

Calibration unlock: separates "1-run game in the 9th inning" leverage
HV (repeatable) from "blowout in the 3rd" volume HV (luck-driven).
Both produce HV but only one is calibration-stable.

Usage:
    python scripts/backfill_wpa.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-wpa-stub")

from app.core import historical_db  # noqa: E402
from scripts._statcast_bulk import load_bulk_statcast  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_wpa")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    try:
        import pandas as pd
    except ImportError:
        log.error("pandas required")
        return 1

    df = load_bulk_statcast(season=args.season)
    if df is None or df.empty:
        log.warning("0 rows written — bulk Statcast unreachable.")
        return 0
    if "delta_home_win_exp" not in df.columns:
        log.warning("delta_home_win_exp column missing")
        return 0

    # Pre-filter to events with WPA data
    sub = df[["game_date", "batter", "delta_home_win_exp"]].copy()
    sub = sub[sub["delta_home_win_exp"].notna()]
    sub["game_date"] = pd.to_datetime(sub["game_date"]).dt.date.astype(str)
    sub["abs_delta"] = sub["delta_home_win_exp"].abs()
    # Per (batter, game_date), sum the absolute deltas while that batter was
    # hitting.  Statcast keys events by `batter` so this captures their
    # at-the-plate WPA contribution.
    grouped = sub.groupby(["batter", "game_date"])["abs_delta"].sum()
    wpa_lookup = {(int(b), d): float(v) for (b, d), v in grouped.items()}

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        cur = conn.execute(
            "SELECT DISTINCT le.slate_date, le.mlb_id "
            "FROM label_event le "
            "WHERE le.label_type = 'highest_value' "
            "ORDER BY le.slate_date, le.mlb_id"
        )
        hv_targets = cur.fetchall()

        if not args.force:
            cur = conn.execute(
                "SELECT slate_date, mlb_id FROM label_event WHERE label_type='wpa'"
            )
            already = {(r["slate_date"], r["mlb_id"]) for r in cur.fetchall()}
            hv_targets = [t for t in hv_targets if (t["slate_date"], t["mlb_id"]) not in already]

        log.info("HV targets to populate WPA: %d", len(hv_targets))
        observed_at = datetime.now(timezone.utc).isoformat()
        upserts = 0
        misses = 0
        for t in hv_targets:
            wpa = wpa_lookup.get((t["mlb_id"], t["slate_date"]))
            if wpa is None:
                misses += 1
                continue
            historical_db.upsert_label_event(
                conn,
                slate_date=t["slate_date"], mlb_id=t["mlb_id"], label_type="wpa",
                label_value=wpa, label_text=None,
                source="pybaseball_statcast",
                observed_at=observed_at,
            )
            upserts += 1
        conn.commit()
        log.info("UPSERT label_event(wpa): %d (no PA data: %d)", upserts, misses)
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
