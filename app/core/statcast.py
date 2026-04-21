"""Statcast kinematics client (wraps pybaseball → Baseball Savant).

V10.1 operational boundary: this module is NEVER called synchronously at
T-65.  Baseball Savant rate-limits aggressive readers and a blocking CSV
pull would hang the pipeline past its lock window.  Use the daily refresh
script (`scripts/refresh_statcast.py`) to bulk-load the season leaderboards
overnight; the T-65 pipeline reads the resulting PlayerStats columns
straight from the DB with zero network.

Metrics exposed: exit velocity, barrel %, hard-hit %, fastball velocity,
induced vertical break, extension, whiff %, chase %.  These are the raw
physical inputs the Real Sports App algorithm rewards (strategy doc
§"Decoding the Alpha").

All fetchers are season-scoped and cached for the duration of a process
run (the refresh job is a single short-lived task; caching prevents
repeated downloads during the cron invocation).
"""

from __future__ import annotations

import logging
from functools import lru_cache

import pandas as pd

logger = logging.getLogger(__name__)


def _col(df: pd.DataFrame, *names: str) -> str | None:
    """Return the first matching column name from `names` that exists in `df`."""
    for n in names:
        if n in df.columns:
            return n
    return None


@lru_cache(maxsize=4)
def _batter_kinematics_table(season: int) -> pd.DataFrame:
    """Fetch season exit-velo + barrels table (all qualified batters).

    Raises the underlying pybaseball exception on network/HTTP errors so the
    caller fails loudly rather than continuing with missing data.
    """
    from pybaseball import statcast_batter_exitvelo_barrels

    df = statcast_batter_exitvelo_barrels(season, minBBE=50)
    logger.info("Statcast batter exitvelo: %d rows for season=%d", len(df), season)
    return df


@lru_cache(maxsize=4)
def _pitcher_percentile_table(season: int) -> pd.DataFrame:
    from pybaseball import statcast_pitcher_percentile_ranks

    df = statcast_pitcher_percentile_ranks(season)
    logger.info("Statcast pitcher percentiles: %d rows for season=%d", len(df), season)
    return df


@lru_cache(maxsize=4)
def _pitcher_arsenal_velocity_table(season: int) -> pd.DataFrame:
    from pybaseball import statcast_pitcher_pitch_arsenal

    df = statcast_pitcher_pitch_arsenal(season, minP=50, arsenal_type="avg_speed")
    logger.info("Statcast pitcher arsenal (velocity): %d rows for season=%d", len(df), season)
    return df


def clear_statcast_cache() -> None:
    """Clear the in-process statcast tables (used between cron invocations)."""
    _batter_kinematics_table.cache_clear()
    _pitcher_percentile_table.cache_clear()
    _pitcher_arsenal_velocity_table.cache_clear()
