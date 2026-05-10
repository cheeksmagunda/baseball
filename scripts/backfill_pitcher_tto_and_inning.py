"""Backfill pitcher TTO + inning-bucket wOBA-against onto player_slate.

Phase E add (May 2026 batter sweep) — addresses two related but distinct
predictive signals from the bulk Statcast pull:

(A) Times-Through-Order (TTO).  A pitcher's wOBA-against on his 2nd
    time through is ~25 wOBA points higher than his 1st; 3rd time is
    ~50 points higher.  Top-of-order batters disproportionately face
    the pitcher in TTO1 and TTO3; mid-order batters face him in TTO1
    only when the pitcher reaches the 2nd inning.  Bucketed by
    `at_bat_number` within each game: PA 1-9 → TTO1, 10-18 → TTO2,
    19+ → TTO3.

(B) Inning bucket wOBA-against.  Top-of-order batters face the
    pitcher in the 1st more than mid-order batters do; relievers face
    pinch-hits and pinch-runs in the 7+.  Bucketed by `inning` value:
    1, 2-3, 4-6, 7+.

Both are derived from the bulk Statcast pull (shared parquet at
scripts/output/.recent_handedness_cache/statcast_*.parquet).  Min 30
PAs per bucket for the rate to be reported; otherwise NULL.

Usage:
    python scripts/backfill_pitcher_tto_and_inning.py
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-pitcher-tto-stub")

from app.core import historical_db  # noqa: E402
from scripts._statcast_bulk import load_bulk_statcast  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_pitcher_tto_and_inning")

MIN_PA_PER_BUCKET = 30


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if importlib.util.find_spec("pandas") is None:
        log.error("pandas required")
        return 1

    df = load_bulk_statcast(season=args.season)
    if df is None or df.empty:
        log.warning("0 rows written — bulk Statcast unreachable.")
        return 0

    woba_col = (
        "estimated_woba_using_speedangle"
        if "estimated_woba_using_speedangle" in df.columns
        else "woba_value"
    )
    needed = ["pitcher", "at_bat_number", "inning", woba_col]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        log.warning("Statcast frame missing columns: %s", missing)
        return 0

    sub = df[needed].copy()
    sub = sub.dropna(subset=["pitcher", "at_bat_number", "inning", woba_col])
    sub["pitcher"] = sub["pitcher"].astype(int)
    sub["at_bat_number"] = sub["at_bat_number"].astype(int)
    sub["inning"] = sub["inning"].astype(int)

    # TTO bucket
    def tto_bucket(ab: int) -> str:
        if ab <= 9:
            return "tto1"
        if ab <= 18:
            return "tto2"
        return "tto3"

    sub["tto"] = sub["at_bat_number"].map(tto_bucket)

    def inning_bucket(inn: int) -> str:
        if inn <= 1:
            return "i1"
        if inn <= 3:
            return "i23"
        if inn <= 6:
            return "i46"
        return "i7p"

    sub["ibkt"] = sub["inning"].map(inning_bucket)

    # Aggregate
    tto_grp = sub.groupby(["pitcher", "tto"]).agg(
        pa=(woba_col, "size"),
        woba=(woba_col, "mean"),
    ).reset_index()
    tto_grp = tto_grp[tto_grp["pa"] >= MIN_PA_PER_BUCKET]
    tto_lookup: dict[int, dict] = {}
    for _, r in tto_grp.iterrows():
        tto_lookup.setdefault(int(r["pitcher"]), {})[
            f"pitcher_{r['tto']}_woba_against"
        ] = round(float(r["woba"]), 4)

    inn_grp = sub.groupby(["pitcher", "ibkt"]).agg(
        pa=(woba_col, "size"),
        woba=(woba_col, "mean"),
    ).reset_index()
    inn_grp = inn_grp[inn_grp["pa"] >= MIN_PA_PER_BUCKET]
    inn_lookup: dict[int, dict] = {}
    for _, r in inn_grp.iterrows():
        col_map = {"i1": "pitcher_woba_inning_1", "i23": "pitcher_woba_inning_2_3",
                   "i46": "pitcher_woba_inning_4_6", "i7p": "pitcher_woba_inning_7plus"}
        inn_lookup.setdefault(int(r["pitcher"]), {})[col_map[r["ibkt"]]] = round(float(r["woba"]), 4)

    log.info(
        "TTO buckets: %d pitchers; inning buckets: %d pitchers",
        len(tto_lookup), len(inn_lookup),
    )

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE position IN ('P','SP','RP','TWP')"
        else:
            where = (
                "WHERE position IN ('P','SP','RP','TWP') "
                "AND pitcher_tto1_woba_against IS NULL AND pitcher_woba_inning_1 IS NULL"
            )
        cur = conn.execute(
            f"SELECT slate_date, mlb_id FROM player_slate {where} "
            "ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        log.info("pitcher rows to populate: %d", len(targets))

        updates = 0
        for t in targets:
            rec: dict = {}
            rec.update(tto_lookup.get(t["mlb_id"], {}))
            rec.update(inn_lookup.get(t["mlb_id"], {}))
            if not rec:
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], rec,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d", updates)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0 and not os.environ.get("HISTORICAL_DB"):
        from scripts.export_historical_csvs import export_all
        export_all()
    sys.exit(rc)
