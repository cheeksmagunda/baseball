"""May 2026 Phase D cleanup migration.

Drops redundant / low-signal columns from slate_game, drops the
umpire_dim table, and lifts venue_* (12 cols) into a new venue_dim
table.

This is a one-shot migration — the SCHEMA_DDL in app/core/historical_db.py
already reflects the post-migration shape, so future fresh DBs come up
clean.  This script brings the existing data/historical.db forward.

Idempotent: re-running on an already-migrated DB is a no-op (each step
checks for the column / table existence first).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "migrate-phase-d-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("migrate_phase_d_cleanup")


COLUMNS_TO_DROP_SLATE_GAME = [
    # Park / standings duplicates
    "park_team",
    "home_team_record_w",
    "home_team_record_l",
    "away_team_record_w",
    "away_team_record_l",
    # Slow-moving / autocorrelated standings noise
    "home_team_streak",
    "away_team_streak",
    "home_team_division_rank",
    "away_team_division_rank",
    "home_team_league_rank",
    "away_team_league_rank",
    # Post-game team-box noise (HR / hits / runs kept)
    "home_team_doubles",
    "home_team_triples",
    "home_team_walks",
    "home_team_strikeouts",
    "home_team_left_on_base",
    "home_team_stolen_bases",
    "home_team_errors",
    "away_team_doubles",
    "away_team_triples",
    "away_team_walks",
    "away_team_strikeouts",
    "away_team_left_on_base",
    "away_team_stolen_bases",
    "away_team_errors",
    # Post-game weather actuals (forecast already in temperature_f / wind_*)
    "actual_temperature_f",
    "actual_wind_speed_mph",
    "actual_wind_direction_deg",
    "actual_precipitation_mm",
    "actual_humidity_pct",
    "actual_pressure_hpa",
    "actual_cloud_cover_pct",
    # Low-signal post-game external observables
    "attendance",
    "day_night",
    # Umpire (2026 ABS Challenge System compresses signal too much)
    "ump_hp_id",
    "ump_hp_name",
    # Venue static dimensions — moving to venue_dim
    "venue_capacity",
    "venue_surface",
    "venue_roof_type",
    "venue_elevation_ft",
    "venue_latitude",
    "venue_longitude",
    "venue_timezone",
    "venue_lf_line_ft",
    "venue_lf_ft",
    "venue_lcf_ft",
    "venue_cf_ft",
    "venue_rcf_ft",
    "venue_rf_ft",
    "venue_rf_line_ft",
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def main() -> int:
    conn = historical_db.connect()
    try:
        # Step 1: Build venue_dim from existing slate_game rows BEFORE dropping
        # the columns we need to copy.
        sg_cols = _columns(conn, "slate_game")
        if "venue_id" in sg_cols and "venue_capacity" in sg_cols:
            log.info("creating venue_dim and copying venue static columns…")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS venue_dim (
                    venue_id            INTEGER PRIMARY KEY,
                    venue_name          TEXT,
                    venue_capacity      INTEGER,
                    venue_surface       TEXT,
                    venue_roof_type     TEXT,
                    venue_elevation_ft  INTEGER,
                    venue_latitude      REAL,
                    venue_longitude     REAL,
                    venue_timezone      TEXT,
                    venue_lf_line_ft    INTEGER,
                    venue_lf_ft         INTEGER,
                    venue_lcf_ft        INTEGER,
                    venue_cf_ft         INTEGER,
                    venue_rcf_ft        INTEGER,
                    venue_rf_ft         INTEGER,
                    venue_rf_line_ft    INTEGER,
                    observed_at         TEXT NOT NULL
                )
                """
            )
            observed_at = datetime.now(timezone.utc).isoformat()
            # Most-recent observed value per venue_id wins (in case a venue
            # changed mid-season, e.g. Athletics).
            conn.execute(
                """
                INSERT OR REPLACE INTO venue_dim (
                    venue_id, venue_name, venue_capacity, venue_surface,
                    venue_roof_type, venue_elevation_ft, venue_latitude,
                    venue_longitude, venue_timezone, venue_lf_line_ft,
                    venue_lf_ft, venue_lcf_ft, venue_cf_ft, venue_rcf_ft,
                    venue_rf_ft, venue_rf_line_ft, observed_at
                )
                SELECT
                    venue_id,
                    MAX(venue_name),
                    MAX(venue_capacity),
                    MAX(venue_surface),
                    MAX(venue_roof_type),
                    MAX(venue_elevation_ft),
                    MAX(venue_latitude),
                    MAX(venue_longitude),
                    MAX(venue_timezone),
                    MAX(venue_lf_line_ft),
                    MAX(venue_lf_ft),
                    MAX(venue_lcf_ft),
                    MAX(venue_cf_ft),
                    MAX(venue_rcf_ft),
                    MAX(venue_rf_ft),
                    MAX(venue_rf_line_ft),
                    ?
                FROM slate_game
                WHERE venue_id IS NOT NULL
                GROUP BY venue_id
                """,
                (observed_at,),
            )
            conn.commit()
            n = conn.execute("SELECT COUNT(*) FROM venue_dim").fetchone()[0]
            log.info("venue_dim populated: %d venues", n)

        # Step 2: Drop the umpire_dim table.
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='umpire_dim'"
        ).fetchone():
            log.info("dropping umpire_dim table")
            conn.execute("DROP TABLE umpire_dim")
            conn.commit()

        # Step 3: Drop columns from slate_game.  SQLite 3.35+ supports
        # ALTER TABLE … DROP COLUMN.
        sg_cols = _columns(conn, "slate_game")
        dropped = 0
        for col in COLUMNS_TO_DROP_SLATE_GAME:
            if col in sg_cols:
                try:
                    conn.execute(f"ALTER TABLE slate_game DROP COLUMN {col}")
                    dropped += 1
                except sqlite3.OperationalError as e:
                    log.warning("DROP COLUMN %s failed: %s", col, e)
        if dropped:
            conn.commit()
            log.info("dropped %d columns from slate_game", dropped)
        else:
            log.info("no columns to drop from slate_game (already migrated)")

        # Step 4: VACUUM to reclaim disk space.
        log.info("VACUUM to reclaim space…")
        conn.execute("VACUUM")
        conn.commit()

        # Final state
        sg_after = _columns(conn, "slate_game")
        log.info("slate_game now has %d columns", len(sg_after))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
