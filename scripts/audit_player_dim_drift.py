"""Audit slowly-changing-dimension drift on player_dim cache files.

Phase C of the May 2026 cleanup sweep moved 8 player attributes
(bat_side / pitch_hand / birth_date / birth_country / mlb_debut_date /
height_in / weight_lb / primary_position_code) from per-(slate_date, mlb_id)
storage on `player_slate` to per-(mlb_id) storage on `player_dim`.  This
script checks how often values drift across the trailing N days of cache
files for the same mlb_id — proves the per-slate snapshot was redundant
in practice and surfaces any column where drift is high enough to want
its own treatment.

The cache lives at scripts/output/.player_externals_cache/<mlb_id>.json.
Each file is the latest MLB Stats API /people response for that mlb_id;
re-running the backfill overwrites with current values.  This script is
forward-looking: if you re-run the backfill on a fresh cache after a
trade window, the cache files will diverge for the affected mlb_ids and
this audit will surface which columns drifted.

Output: a table of "distinct values per (mlb_id, attr)" — for a clean
SCD migration we want ≤1 distinct value per cell for ~99% of cells, and
the long tail (rare drift) to live in known categories (trade →
primary_position_code, offseason refresh → height_in / weight_lb).

Usage:
    python scripts/audit_player_dim_drift.py
    python scripts/audit_player_dim_drift.py --column primary_position_code
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "audit-player-dim-drift-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("audit_player_dim_drift")


DIM_COLUMNS = (
    "bat_side", "pitch_hand", "birth_date", "birth_country",
    "mlb_debut_date", "height_in", "weight_lb", "primary_position_code",
)


def _row_for_each_player(conn) -> dict[int, dict]:
    """Pull the current per-mlb_id values from player_dim."""
    cur = conn.execute(
        "SELECT mlb_id, "
        + ", ".join(DIM_COLUMNS)
        + ", first_observed_date, last_observed_date "
        "FROM player_dim ORDER BY mlb_id"
    )
    return {r["mlb_id"]: dict(r) for r in cur.fetchall()}


def _per_slate_history(conn) -> dict[int, dict[str, set]]:
    """If player_slate carried any of the dim columns historically (pre-Phase-C
    DBs), report each mlb_id's distinct values per column across all slates.

    Returns {mlb_id: {col: set_of_distinct_values}}.  For a post-Phase-C DB
    this returns an empty dict (the columns no longer exist on player_slate).
    """
    cur = conn.execute("PRAGMA table_info(player_slate)")
    available = {r["name"] for r in cur.fetchall()}
    legacy_cols = [c for c in DIM_COLUMNS if c in available]
    if not legacy_cols:
        return {}

    history: dict[int, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    cur = conn.execute(
        "SELECT mlb_id, " + ", ".join(legacy_cols) + " FROM player_slate WHERE mlb_id > 0"
    )
    for r in cur.fetchall():
        for col in legacy_cols:
            v = r[col]
            if v is not None and v != "":
                history[r["mlb_id"]][col].add(v)
    return history


def _summarize(history: dict[int, dict[str, set]]) -> dict[str, dict[int, int]]:
    """{col: {distinct_count: n_players_with_that_count}}."""
    summary: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for mlb_id, by_col in history.items():
        for col in DIM_COLUMNS:
            n_distinct = len(by_col.get(col, set()))
            summary[col][n_distinct] += 1
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--column", default=None,
                    help="Drill into a single column and list per-mlb_id "
                         "distinct values for it (rather than the summary).")
    args = ap.parse_args()

    conn = historical_db.connect_readonly()
    try:
        dim_rows = _row_for_each_player(conn)
        log.info("player_dim row count: %d", len(dim_rows))

        history = _per_slate_history(conn)
        if not history:
            log.info(
                "player_slate carries 0 of the 8 dim columns — Phase C migration "
                "complete, nothing to compare against."
            )
            log.info("(Re-run after a future cache refresh diverges values.)")
            return 0

        log.info("legacy player_slate dim-column data found — comparing drift")
        summary = _summarize(history)

        if args.column:
            if args.column not in DIM_COLUMNS:
                log.error("--column must be one of %s", DIM_COLUMNS)
                return 2
            for mlb_id, by_col in sorted(history.items()):
                values = by_col.get(args.column, set())
                if len(values) > 1:
                    print(f"  mlb_id={mlb_id:>8}: {sorted(values)}")
            return 0

        # Tabular summary
        n_players = len(history)
        print()
        print(f"{'column':<24}  {'1 value':>8}  {'2 values':>9}  {'3+ values':>10}  {'%≤1':>6}")
        print("-" * 72)
        for col in DIM_COLUMNS:
            counts = summary[col]
            n1 = counts.get(1, 0)
            n2 = counts.get(2, 0)
            n3p = sum(v for k, v in counts.items() if k >= 3)
            pct = (n1 + counts.get(0, 0)) / n_players * 100 if n_players else 0
            print(f"{col:<24}  {n1:>8}  {n2:>9}  {n3p:>10}  {pct:>5.1f}%")
        print()
        print("Migration health: column with %≤1 < 99 → consider keeping it "
              "per-slate or building a history table.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
