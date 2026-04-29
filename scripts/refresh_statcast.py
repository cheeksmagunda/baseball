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
from app.core.mlb_api import TEAM_ABBR_BY_MLB_ID
from app.core.statcast import (
    _batter_expected_stats_table,
    _batter_kinematics_table,
    _pitcher_arsenal_velocity_table,
    _pitcher_expected_stats_table,
    _pitcher_movement_table,
    _pitcher_percentile_table,
    _team_catcher_framing_table,
    _col,
)
from app.database import SessionLocal
from app.models.player import Player, PlayerStats, TeamSeasonStats

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
    """Pull the pitcher percentile-ranks + arsenal-velocity + pitch-movement tables and upsert.

    Apr 27 2026 audit: Savant's percentile-ranks endpoint no longer exposes
    `ff_avg_break_z_induced` (IVB) or `avg_extension`.  IVB is recoverable from
    the public `pitch-movement` leaderboard (column `pitcher_break_z_induced`,
    pitch_type=FF), which `_pitcher_movement_table()` fetches via direct CSV.
    Extension is no longer in any standard leaderboard endpoint — staying NULL
    until either Savant restores it or we add a raw `statcast_pitcher`
    aggregator.  The scoring engine routes through 4-of-5 kinematic signals
    in the meantime (still hits the ≥3 kinematic-path threshold).
    """
    perc = _pitcher_percentile_table(season)
    vel = _pitcher_arsenal_velocity_table(season)
    mov = _pitcher_movement_table(season)

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

    # IVB lookup (4-seam fastball, induced vertical break in inches) — pulled
    # straight from the pitch-movement leaderboard since pybaseball's
    # percentile_ranks endpoint stopped exposing it.
    mov_id = _col(mov, "pitcher_id", "player_id", "pitcher")
    ivb_lookup: dict[int, float] = {}
    if mov_id is not None:
        for _, row in mov.iterrows():
            pid = row.get(mov_id)
            if pid in (None, "") or pd.isna(pid):
                continue
            ivb = _safe_float(row.get("pitcher_break_z_induced"))
            if ivb is not None:
                try:
                    ivb_lookup[int(pid)] = ivb
                except (TypeError, ValueError):
                    continue

    updated = 0
    for _, row in perc.iterrows():
        mlb_id = row[perc_id]
        if pd.isna(mlb_id):
            continue
        ps = _upsert_stats_row(db, int(mlb_id), season)
        if ps is None:
            continue

        ps.fb_velocity = vel_lookup.get(int(mlb_id))
        # IVB now sourced from the pitch-movement leaderboard; fall back to the
        # legacy column names in case Savant restores them in percentile_ranks.
        ps.fb_ivb = (
            ivb_lookup.get(int(mlb_id))
            or _safe_float(row.get("ff_avg_break_z_induced"))
            or _safe_float(row.get("fb_ivb"))
        )
        # Extension: no current leaderboard exposes this season-aggregated.
        # Left for a future raw-statcast aggregator.  Will be NULL until then.
        ps.fb_extension = _safe_float(row.get("avg_extension")) or _safe_float(row.get("release_extension"))
        ps.whiff_pct = _safe_float(row.get("whiff_percent"))
        ps.chase_pct = _safe_float(row.get("oz_swing_percent")) or _safe_float(row.get("chase_percent"))
        updated += 1
    db.commit()
    return updated


def refresh_batter_expected_stats(db: Session, season: int) -> int:
    """V10.8 — pull Savant's batter expected-stats leaderboard, upsert
    x_woba/x_ba/x_slg onto PlayerStats."""
    df = _batter_expected_stats_table(season)
    id_col = _col(df, "player_id", "batter", "mlb_id")
    if id_col is None:
        raise RuntimeError(
            f"Batter expected-stats leaderboard missing id column "
            f"(columns: {list(df.columns)[:10]})"
        )
    updated = 0
    for _, row in df.iterrows():
        mlb_id = row[id_col]
        if pd.isna(mlb_id):
            continue
        ps = _upsert_stats_row(db, int(mlb_id), season)
        if ps is None:
            continue
        ps.x_woba = _safe_float(row.get("est_woba"))
        ps.x_ba = _safe_float(row.get("est_ba"))
        ps.x_slg = _safe_float(row.get("est_slg"))
        updated += 1
    db.commit()
    return updated


def refresh_pitcher_expected_stats(db: Session, season: int) -> int:
    """V10.8 — pull Savant's pitcher expected-stats leaderboard, upsert
    x_era and x_woba_against onto PlayerStats."""
    df = _pitcher_expected_stats_table(season)
    id_col = _col(df, "player_id", "pitcher", "mlb_id")
    if id_col is None:
        raise RuntimeError(
            f"Pitcher expected-stats leaderboard missing id column "
            f"(columns: {list(df.columns)[:10]})"
        )
    updated = 0
    for _, row in df.iterrows():
        mlb_id = row[id_col]
        if pd.isna(mlb_id):
            continue
        ps = _upsert_stats_row(db, int(mlb_id), season)
        if ps is None:
            continue
        ps.x_era = _safe_float(row.get("xera"))
        ps.x_woba_against = _safe_float(row.get("est_woba"))
        updated += 1
    db.commit()
    return updated


def refresh_team_catcher_framing(db: Session, season: int) -> int:
    """V10.8 — pull Savant's team-level catcher framing leaderboard, upsert
    framing_runs / framing_strike_pct / framing_pitches onto TeamSeasonStats.

    The Savant page exposes 30 team rows with `team_id`, `pitches`, `rv_tot`,
    `pct_tot`.  We resolve `team_id` → 3-letter abbreviation via TEAM_ABBR_BY_MLB_ID
    and upsert one row per team.

    Fails loud: a fetch or parse failure raises and bubbles up to `main()`,
    which exits non-zero so the slate monitor calls `lineup_cache.mark_failed()`.
    Per CLAUDE.md "fail loud, never fallback" — Savant is fully public and
    always live; a network or schema failure is a real problem that must be
    fixed, not silently swallowed.  The framing adjustment is small (±5% on
    pitcher k_rate) but the system shouldn't lie about which signals fired.

    Returns the count of upserted team rows.  Zero updates is acceptable
    (early-season corner case, no team rows yet); only an actual fetch /
    parse failure aborts.
    """
    df = _team_catcher_framing_table(season)

    updated = 0
    for _, row in df.iterrows():
        team_id = row.get("team_id")
        if team_id in (None, "") or pd.isna(team_id):
            continue
        abbr = TEAM_ABBR_BY_MLB_ID.get(int(team_id))
        if abbr is None:
            continue
        rv_tot = _safe_float(row.get("rv_tot"))
        pct_tot = _safe_float(row.get("pct_tot"))
        pitches_raw = row.get("pitches")
        try:
            pitches = int(pitches_raw) if pitches_raw not in (None, "") else None
        except (TypeError, ValueError):
            pitches = None

        tss = (
            db.query(TeamSeasonStats)
            .filter_by(team=abbr, season=season)
            .first()
        )
        if tss is None:
            tss = TeamSeasonStats(team=abbr, season=season)
            db.add(tss)
        tss.framing_runs = rv_tot
        tss.framing_strike_pct = pct_tot
        tss.framing_pitches = pitches
        from datetime import datetime as _dt
        tss.updated_at = _dt.utcnow()
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
        # V10.8 additions — xStats + team framing.  These are independent
        # tables/columns from the kinematics path, so a partial failure on
        # one doesn't corrupt the others.  The xStats pulls are mandatory
        # (we count them in the sanity floor); framing is best-effort
        # because the team scrape is a separate Savant page.
        batter_xstats_updates = refresh_batter_expected_stats(db, season)
        pitcher_xstats_updates = refresh_pitcher_expected_stats(db, season)
        framing_updates = refresh_team_catcher_framing(db, season)
    finally:
        db.close()

    logger.info(
        "Statcast refresh done: batters kinematics=%d xStats=%d, pitchers kinematics=%d xStats=%d, team framing=%d",
        batter_updates, batter_xstats_updates,
        pitcher_updates, pitcher_xstats_updates,
        framing_updates,
    )
    # Sanity floor: kinematics + xStats must update SOMETHING.  Framing is
    # tolerated at 0 (Savant team-scrape can fail without breaking us; a
    # team-framing miss just means the V10.8 framing adjustment falls
    # through to neutral for that slate cycle).
    if batter_updates == 0 or pitcher_updates == 0:
        logger.error(
            "Statcast refresh produced zero updates on one or both kinematics "
            "leaderboards — inspect column naming and Player.mlb_id coverage."
        )
        return 1
    if batter_xstats_updates == 0 or pitcher_xstats_updates == 0:
        logger.error(
            "Statcast xStats refresh produced zero updates — inspect Savant "
            "expected-stats column names (est_woba, est_ba, est_slg, xera) and "
            "Player.mlb_id coverage."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
