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


@lru_cache(maxsize=4)
def _pitcher_movement_table(season: int) -> pd.DataFrame:
    """Fetch the season pitch-movement leaderboard (4-seam IVB, in inches).

    Apr 27 2026 audit: pybaseball's `statcast_pitcher_percentile_ranks` no
    longer exposes `ff_avg_break_z_induced` (induced vertical break) — Savant
    moved the metric out of the percentile feed.  IVB still lives on the
    public pitch-movement leaderboard, so we fetch its CSV directly via HTTP.

    Filters to pitch_type=FF and returns columns:
      - pitcher_id (int)
      - pitcher_break_z_induced (float, inches of induced vertical break)

    Caller is responsible for joining onto the percentile table by player_id.
    Raises on non-200 / parse error so the caller fails loudly.
    """
    import csv
    import io

    import requests

    url = (
        "https://baseballsavant.mlb.com/leaderboard/pitch-movement"
        f"?year={season}&team=&min_pitches=q&pitch_type=FF&hand=&csv=true"
    )
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    # Savant prepends a UTF-8 BOM ("﻿\"year\"") on the first column header.
    text = resp.text
    rows = list(csv.DictReader(io.StringIO(text)))
    df = pd.DataFrame(rows)
    # Strip the BOM/quote wrapping from the first column name if present.
    df.columns = [c.lstrip("﻿").strip('"') for c in df.columns]
    logger.info(
        "Statcast pitcher movement (IVB): %d rows for season=%d (pitch_type=FF)",
        len(df), season,
    )
    return df


@lru_cache(maxsize=4)
def _batter_expected_stats_table(season: int) -> pd.DataFrame:
    """Fetch the batter expected-stats leaderboard (xwOBA, xBA, xSLG).

    V10.8 — pybaseball wraps Savant's expected_statistics endpoint; the
    `est_woba`, `est_ba`, `est_slg` columns are the industry-standard
    contact-quality leading indicators.  See MLB Glossary on xwOBA:
    https://www.mlb.com/glossary/statcast/expected-woba

    Used for the batter `power_profile` trait — xwOBA is more predictive
    than realised wOBA for forward-looking RS estimation, especially on
    small samples.
    """
    from pybaseball import statcast_batter_expected_stats

    df = statcast_batter_expected_stats(season, minPA=50)
    logger.info("Statcast batter expected stats: %d rows for season=%d", len(df), season)
    return df


@lru_cache(maxsize=4)
def _pitcher_expected_stats_table(season: int) -> pd.DataFrame:
    """Fetch the pitcher expected-stats leaderboard (xwOBA-against, xERA).

    V10.8 — xERA is a 1:1 conversion of xwOBA-against onto the ERA scale.
    Wide live-ERA-vs-xERA gaps are screaming regression signals (per
    FantasyLabs / PitcherList DFS literature).  Used as the simplified
    pitch-arsenal-mismatch proxy: a single number that captures whether
    the pitcher's overall arsenal is suppressing contact quality.
    """
    from pybaseball import statcast_pitcher_expected_stats

    df = statcast_pitcher_expected_stats(season, minPA=50)
    logger.info("Statcast pitcher expected stats: %d rows for season=%d", len(df), season)
    return df


@lru_cache(maxsize=4)
def _team_catcher_framing_table(season: int) -> pd.DataFrame:
    """Fetch Savant's TEAM-level catcher framing leaderboard.

    V10.8 — pybaseball's `statcast_catcher_framing()` returns an empty body
    on Savant's current schema, so we scrape the embedded JSON from the
    leaderboard HTML directly.  The `data` script variable holds 30 team
    rows with `team_id`, `pitches`, `pitches_shadow`, `rv_tot`, `pct_tot`,
    plus per-zone breakdowns.

    `rv_tot` is the team's total framing runs added (or saved) for the
    season — positive = good framing, negative = bad.  Reduced impact
    under the 2026 ABS Challenge System (challenges fix the worst calls)
    but still meaningful for the ~98% of pitches that aren't challenged
    (per ESPN/MLB.com 2026 ABS announcement).

    Returns a DataFrame with columns: team_id, pitches, rv_tot, pct_tot.
    Caller joins to TEAM_MLB_IDS reverse lookup to get the abbreviation.
    """
    import json
    import re

    import requests

    url = (
        "https://baseballsavant.mlb.com/leaderboard/catcher-framing"
        f"?year={season}&team=&min=q&type=Cat"
    )
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        timeout=20,
    )
    resp.raise_for_status()
    # Embedded JSON pattern: `data = [...];`
    match = re.search(r"\bdata\s*=\s*(\[.*?\]);", resp.text, re.DOTALL)
    if not match:
        raise RuntimeError(
            "Savant catcher-framing leaderboard: embedded `data` JSON variable not "
            "found — page schema may have changed.  Inspect the raw HTML."
        )
    rows = json.loads(match.group(1))
    df = pd.DataFrame(rows)
    logger.info("Statcast team catcher framing: %d rows for season=%d", len(df), season)
    return df


def clear_statcast_cache() -> None:
    """Clear the in-process statcast tables (used between cron invocations)."""
    _batter_kinematics_table.cache_clear()
    _pitcher_percentile_table.cache_clear()
    _pitcher_arsenal_velocity_table.cache_clear()
    _pitcher_movement_table.cache_clear()
    _batter_expected_stats_table.cache_clear()
    _pitcher_expected_stats_table.cache_clear()
    _team_catcher_framing_table.cache_clear()
