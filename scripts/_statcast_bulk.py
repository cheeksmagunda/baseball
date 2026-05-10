"""Shared helper: load the bulk season-wide Statcast pull as a pandas DataFrame.

Several Tier-3 backfills (recent_handedness_splits, statcast_pa, wpa,
batter_pitch_type_splits) all need the same per-pitch event data.
Calling pybaseball.statcast() once per script is wasteful — the bulk
pull is ~30s and ~200k events.  This module provides a single cached
parquet file at scripts/output/.recent_handedness_cache/statcast_<season>.parquet
that all callers can mmap-load.

Usage:
    from scripts._statcast_bulk import load_bulk_statcast
    df = load_bulk_statcast(season=2026)  # returns pandas DataFrame
"""
from __future__ import annotations

import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "scripts" / "output" / ".recent_handedness_cache"

log = logging.getLogger(__name__)


def load_bulk_statcast(season: int = 2026, force_refetch: bool = False):
    """Return the season-wide Statcast events DataFrame, fetching once if cold."""
    try:
        import pandas as pd
    except ImportError:
        log.error("pandas not installed")
        return None

    season_start = f"{season}-03-01"
    season_end = f"{season}-11-15"
    cache_file = CACHE_DIR / f"statcast_{season_start}_{season_end}.parquet"

    if cache_file.exists() and not force_refetch:
        try:
            df = pd.read_parquet(cache_file)
            log.info("loaded cached bulk statcast: %d events", len(df))
            return df
        except Exception as e:
            log.warning("cache read failed: %s", e)

    try:
        from pybaseball import statcast
    except ImportError:
        log.error("pybaseball not installed")
        return None

    log.info("bulk fetching statcast %s → %s (1-2 min)…", season_start, season_end)
    try:
        df = statcast(start_dt=season_start, end_dt=season_end, verbose=False)
    except Exception as e:
        log.warning("bulk statcast fetch failed: %s", e)
        return None
    if df is None or df.empty:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(cache_file)
    except Exception as e:
        log.warning("parquet cache write failed: %s", e)
    log.info("bulk statcast loaded: %d events", len(df))
    return df
