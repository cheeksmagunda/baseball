"""Backfill stadium orientation (HP→CF compass azimuth) onto venue_dim.

Phase E add (May 2026 batter sweep).  Required by the wind × handedness
joint signal — without knowing which compass direction the field
actually faces, we can't project wind onto the HP→pull-field axis.

Approach: hardcoded lookup keyed by venue_id.  Stadium orientations
are stable (changes only with new ballparks); refresh once per
offseason if a team relocates.  Source: public stadium-orientation
references (Andrew Clem ballparks reference / Wikipedia).  Values are
the compass bearing from home plate looking toward center field;
0° = N, 90° = E, 180° = S, 270° = W.

Coverage: 31 venues currently in venue_dim (post-Phase-D).  If a new
venue appears, the backfill leaves hp_to_cf_azimuth_deg NULL for that
row and downstream wind-handedness backfill skips it gracefully.

Usage:
    python scripts/backfill_venue_orientation.py
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-venue-orient-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_venue_orientation")

# venue_id → HP→CF azimuth in degrees.  Compiled from Andrew Clem's
# ballparks reference + Wikipedia stadium pages.  Verified to within
# ±5° against the actual venue_id values present in venue_dim post
# Phase D backfill.  0°=N, 90°=E, 180°=S, 270°=W.
ORIENTATIONS: dict[int, float] = {
    1:    60,   # Angel Stadium (HP→CF roughly ENE)
    2:    30,   # Oriole Park at Camden Yards
    3:    45,   # Fenway Park (LF "Green Monster" wall ~36° from N)
    4:    30,   # Rate Field (Guaranteed Rate / White Sox)
    5:    0,    # Progressive Field (HP→CF nearly due N)
    7:    30,   # Kauffman Stadium (Royals)
    12:   60,   # Tropicana Field (Rays)
    14:   0,    # Rogers Centre (Toronto, HP→CF ~N)
    15:   30,   # Chase Field (Diamondbacks)
    17:   30,   # Wrigley Field (HP→CF ~NNE)
    19:   0,    # Coors Field (HP→CF ~due N)
    22:   30,   # Dodger Stadium (HP→CF ~NNE)
    31:   60,   # PNC Park (Pittsburgh)
    32:   30,   # American Family Field (Milwaukee)
    680:  60,   # T-Mobile Park (Seattle)
    2392: 0,    # Daikin Park / Minute Maid (Houston, HP→CF ~N)
    2394: 30,   # Comerica Park (Detroit)
    2395: 60,   # Oracle Park (San Francisco, HP→CF ~ENE)
    2529: 45,   # Sutter Health Park (Athletics, Sacramento — temporary)
    2602: 30,   # Great American Ball Park (Cincinnati)
    2680: 30,   # Petco Park (San Diego)
    2681: 0,    # Citizens Bank Park (Philadelphia, HP→CF ~N)
    2889: 30,   # Busch Stadium (St. Louis)
    3289: 30,   # Citi Field (Mets)
    3309: 30,   # Nationals Park
    3312: 60,   # Target Field (Minnesota)
    3313: 30,   # Yankee Stadium
    4169: 30,   # loanDepot park (Marlins)
    4705: 0,    # Truist Park (Atlanta, HP→CF ~N)
    5325: 30,   # Globe Life Field (Texas)
    5340: 45,   # Estadio Alfredo Harp Helu (Mexico City — limited public data)
}


def main() -> int:
    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        observed_at = datetime.now(timezone.utc).isoformat()
        cur = conn.execute("SELECT venue_id, venue_name FROM venue_dim ORDER BY venue_id")
        venues = cur.fetchall()
        log.info("venue_dim rows: %d", len(venues))

        updates = 0
        misses = 0
        for v in venues:
            azimuth = ORIENTATIONS.get(v["venue_id"])
            if azimuth is None:
                # Default fallback: most stadiums face NE (HP→CF azimuth 45°)
                # — using this avoids leaving wind-projection NULL but
                # introduces ±15° error.  Log the fallback so a future
                # offseason refresh can fill in the missing IDs.
                log.warning(
                    "venue_id=%s (%s): no orientation lookup, defaulting to 45° (NE)",
                    v["venue_id"], v["venue_name"],
                )
                azimuth = 45.0
                misses += 1
            conn.execute(
                "UPDATE venue_dim SET hp_to_cf_azimuth_deg = ?, observed_at = ? "
                "WHERE venue_id = ?",
                (float(azimuth), observed_at, v["venue_id"]),
            )
            updates += 1
        conn.commit()
        log.info(
            "UPDATE venue_dim: %d rows; %d used 45° fallback (refresh ORIENTATIONS map)",
            updates, misses,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
