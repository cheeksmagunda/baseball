"""Statcast kinematics client (wraps pybaseball → Baseball Savant).

V10.0 — pulls season-aggregate Statcast metrics the MLB Stats API does not
expose: exit velocity, barrel %, hard-hit %, fastball velocity, induced
vertical break, extension, whiff %, chase %.

These are the raw physical inputs the Real Sports App algorithm rewards
(strategy doc §"Decoding the Alpha").  They must be sourced live — the
CLAUDE.md "no fallbacks" and "no historical outcome data as input" rules
apply: if Baseball Savant is unreachable, raise loudly rather than feeding
stale or synthetic values into scoring.

All fetchers are season-scoped and cached for the duration of a process run
(the T-65 pipeline is a single short-lived task; caching across it prevents
repeated downloads of the same CSV during a slate).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class BatterKinematics:
    avg_exit_velocity: float | None = None
    max_exit_velocity: float | None = None
    hard_hit_pct: float | None = None
    barrel_pct: float | None = None


@dataclass
class PitcherKinematics:
    fb_velocity: float | None = None
    fb_ivb: float | None = None
    fb_extension: float | None = None
    whiff_pct: float | None = None
    chase_pct: float | None = None


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


def get_batter_kinematics(mlb_id: int, season: int) -> BatterKinematics:
    """Return Statcast kinematics for a single batter.

    Missing rows (player hasn't met the 50-BBE threshold yet) return a
    BatterKinematics with all-None fields — the scoring engine treats these
    exactly the same as a rookie with no MLB samples.  This is NOT a
    fallback: it is the honest statement "no Statcast signal available".
    """
    df = _batter_kinematics_table(season)
    id_col = _col(df, "player_id", "batter", "mlb_id")
    if id_col is None:
        raise RuntimeError(
            f"Statcast batter table missing expected id column (got {list(df.columns)[:5]}...)"
        )
    row = df[df[id_col] == mlb_id]
    if row.empty:
        return BatterKinematics()
    r = row.iloc[0]

    def _f(name: str) -> float | None:
        if name not in df.columns:
            return None
        v = r[name]
        return float(v) if pd.notna(v) else None

    return BatterKinematics(
        avg_exit_velocity=_f("avg_hit_speed") or _f("avg_ev") or _f("launch_speed"),
        max_exit_velocity=_f("max_hit_speed") or _f("max_ev"),
        hard_hit_pct=_f("ev95percent") or _f("hard_hit_percent"),
        barrel_pct=_f("brl_percent") or _f("barrel_batted_rate"),
    )


def get_pitcher_kinematics(mlb_id: int, season: int) -> PitcherKinematics:
    """Return Statcast kinematics for a single pitcher."""
    perc = _pitcher_percentile_table(season)
    vel = _pitcher_arsenal_velocity_table(season)

    perc_id = _col(perc, "player_id", "pitcher", "mlb_id")
    if perc_id is None:
        raise RuntimeError(
            f"Statcast pitcher percentile table missing id column (got {list(perc.columns)[:5]}...)"
        )
    prow = perc[perc[perc_id] == mlb_id]

    def _perc(name: str) -> float | None:
        if prow.empty or name not in perc.columns:
            return None
        v = prow.iloc[0][name]
        return float(v) if pd.notna(v) else None

    vel_id = _col(vel, "pitcher", "player_id", "mlb_id")
    fb_velocity = None
    if vel_id is not None:
        vrow = vel[vel[vel_id] == mlb_id]
        if not vrow.empty:
            # Baseball Savant exposes fastball-type columns such as ff_avg_speed
            # (4-seam), sinker_avg_speed, etc.  4-seam drives K/9 via IVB illusion,
            # so 4-seam velo is the primary signal.  Column names shift across
            # pybaseball releases — probe a few known variants.
            for col in ("ff_avg_speed", "4-Seamer", "4-Seam Fastball", "fastball_avg_speed"):
                if col in vel.columns and pd.notna(vrow.iloc[0][col]):
                    fb_velocity = float(vrow.iloc[0][col])
                    break

    return PitcherKinematics(
        fb_velocity=fb_velocity,
        fb_ivb=_perc("ff_avg_break_z_induced") or _perc("fb_ivb"),
        fb_extension=_perc("avg_extension") or _perc("release_extension"),
        whiff_pct=_perc("whiff_percent"),
        chase_pct=_perc("oz_swing_percent") or _perc("chase_percent"),
    )


def clear_statcast_cache() -> None:
    """Clear the in-process statcast tables (used between T-65 runs if desired)."""
    _batter_kinematics_table.cache_clear()
    _pitcher_percentile_table.cache_clear()
    _pitcher_arsenal_velocity_table.cache_clear()
