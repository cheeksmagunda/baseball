"""Historical calibration corpus — SQLite schema and connection helpers.

The historical store at `data/historical.db` is the canonical source of truth
for the calibration corpus (leaderboard outcomes, winning lineups, at-slate
inputs, prior game logs, slate-level env signals).  CSVs/JSON in /data/ are
byte-stable derived exports refreshed on every write — they remain
human-readable for ad-hoc inspection but the runtime/calibration paths read
from SQLite.

Five logical tables:
  - slate            — one row per slate envelope (date, game count, etc.)
  - slate_game       — one row per (slate_date, game_pk); env signals + post-game
  - player_slate     — one row per (slate_date, mlb_id); identity + at-slate inputs
  - player_game_log  — one row per (slate_date, mlb_id, game_date); prior-game
                       outcomes that feed recent_form / hot_streak calibration
  - label_event      — one row per (slate_date, mlb_id, label_type, source);
                       the typed/sourced outcome label store.  The presence or
                       absence of a row IS the signal — replaces empty-cell
                       semantics from the CSV era.

This module is in `app/core/` because both calibration scripts and the live
runtime (`app/core/popularity.py` after Step 5) need it.  The ONLY caller in
`app/` permitted to query outcome labels is `popularity.py`, and only for the
prior-slate `most_popular` flag — exactly the same carve-out the CSV era had.
The audit script `scripts/audit_live_isolation.py` enforces this.

The schema is deliberately permissive (no NOT NULL beyond PKs, no CHECKs, JSON
held as TEXT) so that:
  * Backfills can run incrementally without staging.
  * Synthetic / future-derived outcome labels can land as new label_type values
    without schema migrations.
  * A row in `player_slate` can exist without any matching `label_event` rows
    (the "did not appear on a leaderboard" case that the CSV could not express
    distinctly from "DNP").
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "historical.db"


def resolve_db_path(override: str | os.PathLike | None = None) -> Path:
    """Resolve the canonical DB path with override precedence.

    Order: explicit `override` arg → `HISTORICAL_DB` env var → `DEFAULT_DB_PATH`.
    The env var is the seam tests + the synthetic-multiplier scaling check
    (Step 7) use to point readers at a non-default DB without touching code.
    """
    if override is not None:
        return Path(override)
    env = os.environ.get("HISTORICAL_DB")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS slate (
    slate_date    TEXT PRIMARY KEY,
    game_count    INTEGER NOT NULL,
    num_brawlers  INTEGER,
    season_stage  TEXT,
    source        TEXT,
    saved_at      TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS slate_game (
    slate_date                  TEXT NOT NULL,
    game_pk                     INTEGER NOT NULL,
    home_team                   TEXT NOT NULL,
    away_team                   TEXT NOT NULL,
    -- starter env signals
    home_starter_id             INTEGER,
    home_starter_name           TEXT,
    home_starter_hand           TEXT,
    home_starter_era            REAL,
    home_starter_whip           REAL,
    home_starter_k_per_9        REAL,
    home_starter_x_era          REAL,
    home_starter_x_woba_against REAL,
    away_starter_id             INTEGER,
    away_starter_name           TEXT,
    away_starter_hand           TEXT,
    away_starter_era            REAL,
    away_starter_whip           REAL,
    away_starter_k_per_9        REAL,
    away_starter_x_era          REAL,
    away_starter_x_woba_against REAL,
    -- team season env signals
    home_team_ops               REAL,
    home_team_k_pct             REAL,
    home_bullpen_era            REAL,
    home_team_framing_runs      REAL,
    home_team_framing_pct       REAL,
    away_team_ops               REAL,
    away_team_k_pct             REAL,
    away_bullpen_era            REAL,
    away_team_framing_runs      REAL,
    away_team_framing_pct       REAL,
    home_team_record_w          INTEGER,
    home_team_record_l          INTEGER,
    home_team_rest_days         INTEGER,
    away_team_record_w          INTEGER,
    away_team_record_l          INTEGER,
    away_team_rest_days         INTEGER,
    home_l10_wins               INTEGER,
    home_series_wins            INTEGER,
    away_l10_wins               INTEGER,
    away_series_wins            INTEGER,
    -- Vegas
    vegas_total                 REAL,
    home_moneyline              INTEGER,
    away_moneyline              INTEGER,
    -- park / weather
    park_team                   TEXT,
    park_hr_factor              REAL,
    temperature_f               REAL,
    wind_speed_mph              REAL,
    wind_direction              TEXT,
    wind_direction_deg          INTEGER,
    datetime_utc                TEXT,
    -- post-game outcomes
    home_score                  INTEGER,
    away_score                  INTEGER,
    winner                      TEXT,
    loser                       TEXT,
    winner_score                INTEGER,
    loser_score                 INTEGER,
    PRIMARY KEY (slate_date, game_pk),
    FOREIGN KEY (slate_date) REFERENCES slate(slate_date)
);
CREATE INDEX IF NOT EXISTS idx_slate_game_pk ON slate_game(game_pk);

CREATE TABLE IF NOT EXISTS player_slate (
    slate_date              TEXT NOT NULL,
    mlb_id                  INTEGER NOT NULL,
    player_name             TEXT NOT NULL,
    team                    TEXT NOT NULL,
    position                TEXT NOT NULL,
    game_pk                 INTEGER,
    -- at-slate inputs (the live pipeline reads these pre-game)
    ops_at_slate            REAL,
    iso_at_slate            REAL,
    era_at_slate            REAL,
    whip_at_slate           REAL,
    k9_at_slate             REAL,
    ops_vs_lhp_at_slate     REAL,
    ops_vs_rhp_at_slate     REAL,
    batting_order_at_slate  INTEGER,
    -- Statcast snapshot at slate
    x_woba                  REAL,
    x_ba                    REAL,
    x_slg                   REAL,
    avg_ev                  REAL,
    hard_hit_pct            REAL,
    barrel_pct              REAL,
    max_ev                  REAL,
    x_era                   REAL,
    x_woba_against          REAL,
    fb_velo                 REAL,
    whiff_pct               REAL,
    chase_pct               REAL,
    fb_ivb                  REAL,
    fb_extension            REAL,
    PRIMARY KEY (slate_date, mlb_id),
    FOREIGN KEY (slate_date, game_pk) REFERENCES slate_game(slate_date, game_pk)
);
CREATE INDEX IF NOT EXISTS idx_player_slate_mlb_id ON player_slate(mlb_id);
CREATE INDEX IF NOT EXISTS idx_player_slate_team ON player_slate(slate_date, team);

CREATE TABLE IF NOT EXISTS player_game_log (
    slate_date    TEXT NOT NULL,
    mlb_id        INTEGER NOT NULL,
    game_date     TEXT NOT NULL,
    player_name   TEXT,
    team          TEXT,
    position      TEXT,
    opponent      TEXT,
    is_home       INTEGER,
    ab            INTEGER,
    runs          INTEGER,
    hits          INTEGER,
    hr            INTEGER,
    rbi           INTEGER,
    bb            INTEGER,
    so            INTEGER,
    sb            INTEGER,
    ip            REAL,
    er            INTEGER,
    k_pitching    INTEGER,
    decision      TEXT,
    PRIMARY KEY (slate_date, mlb_id, game_date)
);
CREATE INDEX IF NOT EXISTS idx_player_game_log_game_date ON player_game_log(game_date);
CREATE INDEX IF NOT EXISTS idx_player_game_log_mlb_id  ON player_game_log(mlb_id);

CREATE TABLE IF NOT EXISTS label_event (
    slate_date   TEXT NOT NULL,
    mlb_id       INTEGER NOT NULL,
    label_type   TEXT NOT NULL,
    label_value  REAL,
    label_text   TEXT,
    source       TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    PRIMARY KEY (slate_date, mlb_id, label_type, source)
);
CREATE INDEX IF NOT EXISTS idx_label_event_type   ON label_event(label_type);
CREATE INDEX IF NOT EXISTS idx_label_event_player ON label_event(slate_date, mlb_id);
CREATE INDEX IF NOT EXISTS idx_label_event_date_type ON label_event(slate_date, label_type);

-- Side table: alias rows used to recover identity for HV box-score players
-- whose canonical name does not match historical_player_game_logs.csv.  Empty
-- by default; populated only when the build script encounters a name that
-- needs an mlb_id alias.
CREATE TABLE IF NOT EXISTS player_alias (
    name_normalized TEXT NOT NULL,
    team            TEXT NOT NULL,
    mlb_id          INTEGER NOT NULL,
    source          TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    PRIMARY KEY (name_normalized, team)
);
"""


# ---------------------------------------------------------------------------
# Label-type vocabulary (audit Section F)
# ---------------------------------------------------------------------------
# Numeric scalar labels — label_value populated, label_text null.
LABEL_TYPES_NUMERIC = (
    "real_score",
    "total_value",
    "card_boost",
    "drafts",
    "total_mult",
    "draft_count",
    "avg_draft_slot",
    "avg_draft_mult",
    "avg_draft_tv",
    "highest_draft_tv",
)

# Boolean-flag leaderboard memberships — label_value=1.0 when the player landed
# on the leaderboard for that slate; absence of a row means "not on it".
LABEL_TYPES_FLAG = (
    "highest_value",
    "most_popular",
    "most_drafted_3x",
)

# Categorical / ordinal — label_text populated, label_value optionally too.
LABEL_TYPES_CATEGORICAL = (
    "most_common_slot",
    "injury_status",
    "winning_lineup_slot",
    "box_score",
)

LABEL_TYPES_ALL = LABEL_TYPES_NUMERIC + LABEL_TYPES_FLAG + LABEL_TYPES_CATEGORICAL

# Sources we currently emit — used by the auditor + the export step to know
# which (label_type, source) tuples produce CSV columns.
SOURCE_REALSPORTS_STATS = "realsports_stats"
SOURCE_REALSPORTS_ENTRIES = "realsports_entries"
SOURCE_MLB_BOXSCORE = "mlb_boxscore"
SOURCE_BACKFILL_RICH = "backfill_rich_stats"
SOURCE_BACKFILL_CARD_BOOST = "backfill_card_boost_and_drafts"
SOURCE_INITIAL_BUILD = "initial_csv_ingest"


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------
def connect(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open a read-write connection to the historical store.

    Caller is responsible for `commit()` / `close()`.  WAL is enabled for
    concurrent-reader safety even when calibration scripts run alongside the
    daily writer.
    """
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def connect_readonly(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open a read-only connection.  Used by audit / calibration / runtime
    paths that must never mutate the corpus.

    Note: SQLite requires the file to already exist for `mode=ro`.  Tests that
    construct fresh DBs should use `connect()` instead.
    """
    path = resolve_db_path(db_path)
    if not path.exists():
        raise FileNotFoundError(
            f"historical_db.connect_readonly: {path} does not exist.  "
            "Run scripts/build_historical_db.py to build the corpus."
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """Idempotently install the full schema.  Safe to call against an existing
    DB — every CREATE uses IF NOT EXISTS."""
    conn.executescript(SCHEMA_DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# Idempotent upsert helpers — used by the daily writer (Step 2), the
# backfill scripts (Step 3), and the audit/calibration readers (Step 4).
# ---------------------------------------------------------------------------
def upsert_slate(conn: sqlite3.Connection, row: dict) -> None:
    cols = ("slate_date", "game_count", "num_brawlers", "season_stage",
            "source", "saved_at", "notes")
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO slate ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(row.get(c) for c in cols),
    )


def upsert_slate_game(conn: sqlite3.Connection, row: dict) -> None:
    """INSERT OR REPLACE on (slate_date, game_pk).  Caller passes a dict with
    any subset of the slate_game columns; missing columns become NULL on
    insert.  For partial backfills, prefer `update_slate_game_columns`."""
    cols = list(_table_columns(conn, "slate_game"))
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO slate_game ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(row.get(c) for c in cols),
    )


def update_slate_game_columns(
    conn: sqlite3.Connection,
    slate_date: str,
    game_pk: int,
    updates: dict,
) -> None:
    """Surgical update of a subset of slate_game columns.  Used by the backfills
    that enrich existing rows (env conditions, handedness, V10.8 signals)
    without disturbing fields populated by other backfills."""
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    params = tuple(updates.values()) + (slate_date, game_pk)
    conn.execute(
        f"UPDATE slate_game SET {set_clause} WHERE slate_date = ? AND game_pk = ?",
        params,
    )


def upsert_player_slate(conn: sqlite3.Connection, row: dict) -> None:
    cols = list(_table_columns(conn, "player_slate"))
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO player_slate ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(row.get(c) for c in cols),
    )


def update_player_slate_columns(
    conn: sqlite3.Connection,
    slate_date: str,
    mlb_id: int,
    updates: dict,
) -> None:
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    params = tuple(updates.values()) + (slate_date, mlb_id)
    conn.execute(
        f"UPDATE player_slate SET {set_clause} WHERE slate_date = ? AND mlb_id = ?",
        params,
    )


def upsert_player_game_log(conn: sqlite3.Connection, row: dict) -> None:
    cols = list(_table_columns(conn, "player_game_log"))
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO player_game_log ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(row.get(c) for c in cols),
    )


def upsert_label_event(
    conn: sqlite3.Connection,
    *,
    slate_date: str,
    mlb_id: int,
    label_type: str,
    label_value: float | None = None,
    label_text: str | None = None,
    source: str,
    observed_at: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO label_event "
        "(slate_date, mlb_id, label_type, label_value, label_text, source, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (slate_date, mlb_id, label_type, label_value, label_text, source, observed_at),
    )


def upsert_player_alias(
    conn: sqlite3.Connection,
    *,
    name_normalized: str,
    team: str,
    mlb_id: int,
    source: str,
    observed_at: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO player_alias "
        "(name_normalized, team, mlb_id, source, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name_normalized, team, mlb_id, source, observed_at),
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Read helpers — thin wrappers; keep query strings centralised.
# ---------------------------------------------------------------------------
def fetch_player_slate_rows(
    conn: sqlite3.Connection,
    slate_date: str | None = None,
) -> list[sqlite3.Row]:
    """Return all player_slate rows, optionally filtered to one slate_date."""
    if slate_date is None:
        cur = conn.execute(
            "SELECT * FROM player_slate ORDER BY slate_date, mlb_id"
        )
    else:
        cur = conn.execute(
            "SELECT * FROM player_slate WHERE slate_date = ? ORDER BY mlb_id",
            (slate_date,),
        )
    return cur.fetchall()


def fetch_label_value(
    conn: sqlite3.Connection,
    slate_date: str,
    mlb_id: int,
    label_type: str,
) -> tuple[float | None, str | None] | None:
    """Return (label_value, label_text) for the matching row, or None.

    When multiple sources have written the same label_type for the same
    (slate_date, mlb_id), the most-recently observed wins.
    """
    cur = conn.execute(
        "SELECT label_value, label_text FROM label_event "
        "WHERE slate_date = ? AND mlb_id = ? AND label_type = ? "
        "ORDER BY observed_at DESC LIMIT 1",
        (slate_date, mlb_id, label_type),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return (row[0], row[1])


def has_label(
    conn: sqlite3.Connection,
    slate_date: str,
    mlb_id: int,
    label_type: str,
) -> bool:
    """True if any label of the given type exists for (slate_date, mlb_id)."""
    cur = conn.execute(
        "SELECT 1 FROM label_event "
        "WHERE slate_date = ? AND mlb_id = ? AND label_type = ? LIMIT 1",
        (slate_date, mlb_id, label_type),
    )
    return cur.fetchone() is not None


def fetch_most_popular_index(
    conn: sqlite3.Connection,
    *,
    cutoff_inclusive: str,
    as_of_exclusive: str,
) -> list[sqlite3.Row]:
    """Read the rolling most_popular fame index used by app/core/popularity.py
    after Step 5.  Returns one row per (slate_date, mlb_id) appearance in the
    leaderboard window; the caller computes mp_appearances / total_appearances.

    Window: cutoff_inclusive <= slate_date < as_of_exclusive.

    The numerator (most_popular flag) and denominator (any leaderboard
    appearance) come from the same label_event table; the denominator is the
    UNION of {most_popular, highest_value, most_drafted_3x} flag rows.
    """
    cur = conn.execute(
        """
        WITH appearances AS (
            SELECT DISTINCT slate_date, mlb_id
            FROM label_event
            WHERE label_type IN ('most_popular', 'highest_value', 'most_drafted_3x')
              AND slate_date >= ?
              AND slate_date < ?
        ),
        mp AS (
            SELECT DISTINCT slate_date, mlb_id, 1 AS mp_flag
            FROM label_event
            WHERE label_type = 'most_popular'
              AND slate_date >= ?
              AND slate_date < ?
        )
        SELECT a.slate_date, a.mlb_id,
               COALESCE(mp.mp_flag, 0) AS is_most_popular
        FROM appearances a
        LEFT JOIN mp USING (slate_date, mlb_id)
        ORDER BY a.slate_date, a.mlb_id
        """,
        (cutoff_inclusive, as_of_exclusive, cutoff_inclusive, as_of_exclusive),
    )
    return cur.fetchall()
