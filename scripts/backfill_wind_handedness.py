"""Backfill wind × batter-handedness components onto slate_game.

Phase E add (May 2026 batter sweep).

Projects the pre-game wind vector onto the HP→pull-field axis for each
handedness:
  wind_to_rf_component  — wind speed projected onto HP→RF (helps LHB pull power)
  wind_to_lf_component  — wind speed projected onto HP→LF (helps RHB pull power)

For a stadium with HP→CF azimuth = α (compass deg, N=0/E=90/etc):
  HP→RF axis is at α + 45 (45° clockwise from CF)
  HP→LF axis is at α − 45

Component = wind_speed × cos(wind_dir − pull_axis_dir)

Wind direction in our DB (from Open-Meteo) is the direction wind is
COMING FROM, compass-deg.  To get the direction wind is BLOWING TO, add
180°.  Then dot-product against the pull axis.

Positive component values = wind blowing toward that pull field
(helping the matched handedness); negative values = wind blowing away.

Both columns can be near-zero or negative — that's by design.

Usage:
    python scripts/backfill_wind_handedness.py
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-wind-hand-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_wind_handedness")


def _project(wind_speed: float, wind_dir_deg_from: float, pull_axis_deg: float) -> float:
    """Project the wind vector (blowing FROM `wind_dir_deg_from`) onto the
    given pull-field axis.  Returns scalar wind speed component (mph).
    Positive = wind blowing toward that pull field."""
    wind_dir_to = (wind_dir_deg_from + 180.0) % 360.0
    delta = math.radians(wind_dir_to - pull_axis_deg)
    return wind_speed * math.cos(delta)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE sg.wind_speed_mph IS NOT NULL AND sg.wind_direction_deg IS NOT NULL"
        else:
            where = (
                "WHERE sg.wind_speed_mph IS NOT NULL AND sg.wind_direction_deg IS NOT NULL "
                "AND sg.wind_to_rf_component IS NULL"
            )
        cur = conn.execute(
            f"""
            SELECT sg.slate_date, sg.game_pk, sg.wind_speed_mph, sg.wind_direction_deg,
                   vd.hp_to_cf_azimuth_deg
            FROM slate_game sg
            JOIN venue_dim vd ON vd.venue_id = sg.venue_id
            {where}
            """
        )
        targets = cur.fetchall()
        log.info("targets: %d games", len(targets))

        updates = 0
        misses = 0
        for t in targets:
            azimuth = t["hp_to_cf_azimuth_deg"]
            if azimuth is None:
                misses += 1
                continue
            ws = float(t["wind_speed_mph"])
            wdf = float(t["wind_direction_deg"])
            rf_axis = (azimuth + 45.0) % 360.0
            lf_axis = (azimuth - 45.0) % 360.0
            rec = {
                "wind_to_rf_component": round(_project(ws, wdf, rf_axis), 2),
                "wind_to_lf_component": round(_project(ws, wdf, lf_axis), 2),
            }
            historical_db.update_slate_game_columns(
                conn, t["slate_date"], t["game_pk"], rec,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no orientation: %d)", updates, misses)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0 and not os.environ.get("HISTORICAL_DB"):
        from scripts.export_historical_csvs import export_all
        export_all()
    sys.exit(rc)
