"""Backfill per-batter, per-pitch-type wOBA splits into batter_pitch_type_woba.

Tier 3 D13 of the May 2026 cleanup-and-add sweep.

Source: bulk season Statcast pull (shared with other Tier-3 backfills).
For each (mlb_id, pitch_type) we aggregate season-to-date PA count and
mean estimated wOBA across every event in the season.

Pipeline plumb (separate change, not in this script): replace V10.8's
"simplified xwOBA-against single number" approach in score_batter_matchup
with the full crosstab — for each opposing pitcher, weight the batter's
per-pitch-type wOBA by the pitcher's arsenal usage % to get the true
expected matchup wOBA.

Storage: one row per (slate_date, mlb_id, pitch_type) where the batter
faced ≥10 of that pitch type up to slate_date.  Approximated using a
season-aggregate snapshot (per-slate point-in-time aggregation would
require ~14M rows; the season aggregate captures the matchup signal
since pitch profiles stabilise within the first ~50 PA).

Usage:
    python scripts/backfill_batter_pitch_type_splits.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-batter-pitch-splits-stub")

from app.core import historical_db  # noqa: E402
from scripts._statcast_bulk import load_bulk_statcast  # noqa: E402

# 11 pitch types we already track in the pitcher arsenal columns
PITCH_TYPES = ("FF", "SI", "FC", "SL", "ST", "CU", "KC", "CH", "FS", "KN", "SV")
MIN_PA_PER_PITCH_TYPE = 10

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_batter_pitch_type_splits")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    try:
        import pandas as pd
    except ImportError:
        log.error("pandas required")
        return 1

    df = load_bulk_statcast(season=args.season)
    if df is None or df.empty:
        log.warning("0 rows written — bulk Statcast unreachable.")
        return 0

    # Keep only rows with both a pitch_type and an estimated wOBA
    woba_col = (
        "estimated_woba_using_speedangle"
        if "estimated_woba_using_speedangle" in df.columns
        else "woba_value"
    )
    if woba_col not in df.columns or "pitch_type" not in df.columns:
        log.warning("Statcast frame missing pitch_type or wOBA column")
        return 0

    sub = df[["batter", "pitch_type", woba_col]].copy()
    sub = sub[sub[woba_col].notna() & sub["pitch_type"].notna()]
    sub = sub[sub["pitch_type"].isin(PITCH_TYPES)]

    # Group: (mlb_id, pitch_type) -> {pa_count, mean_woba}
    grouped = sub.groupby(["batter", "pitch_type"]).agg(
        pa_count=(woba_col, "size"),
        woba=(woba_col, "mean"),
    ).reset_index()
    grouped = grouped[grouped["pa_count"] >= MIN_PA_PER_PITCH_TYPE]
    log.info("loaded %d (batter, pitch_type) aggregates", len(grouped))

    # Build mlb_id -> [(pitch_type, pa_count, woba)] map
    by_batter: dict[int, list[tuple[str, int, float]]] = {}
    for _, row in grouped.iterrows():
        by_batter.setdefault(int(row["batter"]), []).append(
            (row["pitch_type"], int(row["pa_count"]), float(row["woba"]))
        )

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        if not args.force:
            cur = conn.execute(
                "SELECT 1 FROM batter_pitch_type_woba WHERE slate_date LIKE ? LIMIT 1",
                (f"{args.season}-%",),
            )
            if cur.fetchone() is not None:
                log.info("season %s already has rows — pass --force to refresh",
                         args.season)
                return 0

        cur = conn.execute(
            "SELECT slate_date, mlb_id FROM player_slate "
            "WHERE position NOT IN ('P','SP','RP','TWP') "
            "ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        log.info("batter-slate targets: %d", len(targets))

        observed_at = datetime.now(timezone.utc).isoformat()
        rows_written = 0
        misses = 0
        for t in targets:
            splits = by_batter.get(t["mlb_id"])
            if not splits:
                misses += 1
                continue
            for pt, pa_count, woba in splits:
                conn.execute(
                    "INSERT OR REPLACE INTO batter_pitch_type_woba "
                    "(slate_date, mlb_id, pitch_type, pa_count, woba, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (t["slate_date"], t["mlb_id"], pt, pa_count, woba, observed_at),
                )
                rows_written += 1
        conn.commit()
        log.info(
            "INSERT batter_pitch_type_woba rows: %d (no splits: %d batters)",
            rows_written, misses,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        if not os.environ.get("HISTORICAL_DB"):
            from scripts.export_historical_csvs import export_all
            export_all()
    sys.exit(rc)
