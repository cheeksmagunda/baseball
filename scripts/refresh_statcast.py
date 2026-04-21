"""Statcast refresh — bulk-load Baseball Savant leaderboards into PlayerStats.

V10.1.1 operational boundary: Baseball Savant must NOT be queried
synchronously at T-65.  Savant rate-limits aggressive readers and a blocking
CSV pull would hang the pipeline past its lock window.  This script fetches
the three season leaderboards ONCE per invocation and upserts the kinematic
columns onto PlayerStats.  The T-65 pipeline then reads them straight from
the DB with zero extra network calls.

Production invocation
---------------------

The slate monitor fires this script automatically at the start of each
Phase 2 (T-65 sleep window) via `_refresh_statcast_background` in
`app/services/slate_monitor.py`.  No Railway cron, no crontab — merge the
code and the next slate cycle triggers the refresh before T-65 fires.

Manual / ad-hoc invocation
--------------------------

    python -m scripts.refresh_statcast                  # use settings.current_season
    python -m scripts.refresh_statcast --season 2026    # explicit override

The "no fallbacks, fail loudly" rule still applies: any scraper failure
exits non-zero so the caller can log it.  Partial-coverage rookies (no row
on the leaderboard yet) keep NULL columns; the scoring engine routes them
through its non-Statcast fallback paths.

Columns written
---------------

    Batter:  avg_exit_velocity, max_exit_velocity, hard_hit_pct, barrel_pct
    Pitcher: fb_velocity, fb_ivb, fb_extension, whiff_pct, chase_pct

Relies on `app.core.statcast` as the single source of truth for column-name
normalization across pybaseball releases.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy.orm import Session

from app.config import settings
from app.core.statcast import (
    _batter_kinematics_table,
    _pitcher_arsenal_velocity_table,
    _pitcher_percentile_table,
    _col,
)
from app.database import SessionLocal
from app.models.player import Player, PlayerStats

logger = logging.getLogger(__name__)


def _safe_float(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _upsert_stats_row(db: Session, mlb_id: int, season: int) -> PlayerStats | None:
    """Fetch-or-skip PlayerStats row keyed by the Player's mlb_id."""
    player = db.query(Player).filter_by(mlb_id=mlb_id).first()
    if not player:
        # Leaderboard rows for players not in our DB are fine — skip them.
        # We only populate stats for players we know about.
        return None
    ps = (
        db.query(PlayerStats)
        .filter_by(player_id=player.id, season=season)
        .first()
    )
    if ps is None:
        ps = PlayerStats(player_id=player.id, season=season)
        db.add(ps)
    return ps


def refresh_batter_kinematics(db: Session, season: int) -> int:
    """Pull the batter exit-velo + barrels leaderboard and upsert every row.

    Returns the count of PlayerStats rows updated.  Raises on table-format
    drift (missing id column) — a loud failure is better than silent NULLs.
    """
    df = _batter_kinematics_table(season)
    id_col = _col(df, "player_id", "batter", "mlb_id")
    if id_col is None:
        raise RuntimeError(
            f"Batter leaderboard missing expected id column (columns: {list(df.columns)[:10]})"
        )
    updated = 0
    for _, row in df.iterrows():
        mlb_id = row[id_col]
        if pd.isna(mlb_id):
            continue
        ps = _upsert_stats_row(db, int(mlb_id), season)
        if ps is None:
            continue
        avg_ev = _safe_float(row.get("avg_hit_speed")) or _safe_float(row.get("avg_ev")) or _safe_float(row.get("launch_speed"))
        max_ev = _safe_float(row.get("max_hit_speed")) or _safe_float(row.get("max_ev"))
        hh_pct = _safe_float(row.get("ev95percent")) or _safe_float(row.get("hard_hit_percent"))
        brl_pct = _safe_float(row.get("brl_percent")) or _safe_float(row.get("barrel_batted_rate"))

        ps.avg_exit_velocity = avg_ev
        ps.max_exit_velocity = max_ev
        ps.hard_hit_pct = hh_pct
        if brl_pct is not None:
            ps.barrel_pct = brl_pct
        updated += 1
    db.commit()
    return updated


def refresh_pitcher_kinematics(db: Session, season: int) -> int:
    """Pull the pitcher percentile-ranks + arsenal-velocity tables and upsert."""
    perc = _pitcher_percentile_table(season)
    vel = _pitcher_arsenal_velocity_table(season)

    perc_id = _col(perc, "player_id", "pitcher", "mlb_id")
    if perc_id is None:
        raise RuntimeError(
            f"Pitcher percentile leaderboard missing id column (columns: {list(perc.columns)[:10]})"
        )

    vel_id = _col(vel, "pitcher", "player_id", "mlb_id")
    vel_lookup: dict[int, float] = {}
    if vel_id is not None:
        for _, row in vel.iterrows():
            pid = row[vel_id]
            if pd.isna(pid):
                continue
            # 4-seam fastball velo is the primary signal; column names shift
            # across pybaseball releases, so we probe a few known variants.
            for col in ("ff_avg_speed", "4-Seamer", "4-Seam Fastball", "fastball_avg_speed"):
                if col in vel.columns:
                    v = _safe_float(row.get(col))
                    if v is not None:
                        vel_lookup[int(pid)] = v
                        break

    updated = 0
    for _, row in perc.iterrows():
        mlb_id = row[perc_id]
        if pd.isna(mlb_id):
            continue
        ps = _upsert_stats_row(db, int(mlb_id), season)
        if ps is None:
            continue

        ps.fb_velocity = vel_lookup.get(int(mlb_id))
        ps.fb_ivb = _safe_float(row.get("ff_avg_break_z_induced")) or _safe_float(row.get("fb_ivb"))
        ps.fb_extension = _safe_float(row.get("avg_extension")) or _safe_float(row.get("release_extension"))
        ps.whiff_pct = _safe_float(row.get("whiff_percent"))
        ps.chase_pct = _safe_float(row.get("oz_swing_percent")) or _safe_float(row.get("chase_percent"))
        updated += 1
    db.commit()
    return updated


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Daily Statcast refresh")
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="Season year (default: settings.current_season).",
    )
    args = parser.parse_args()

    season = args.season or settings.current_season
    started = datetime.now(timezone.utc).isoformat()
    logger.info("Statcast refresh starting (season=%d, utc=%s)", season, started)

    db = SessionLocal()
    try:
        batter_updates = refresh_batter_kinematics(db, season)
        pitcher_updates = refresh_pitcher_kinematics(db, season)
    finally:
        db.close()

    logger.info(
        "Statcast refresh done: batters updated=%d, pitchers updated=%d",
        batter_updates, pitcher_updates,
    )
    # Sanity floor: if EITHER leaderboard returns zero updates, something is
    # wrong (wrong season, schema change, or ID-join mismatch).  Fail loudly.
    if batter_updates == 0 or pitcher_updates == 0:
        logger.error(
            "Statcast refresh produced zero updates on one or both leaderboards — "
            "inspect column naming and Player.mlb_id coverage."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
