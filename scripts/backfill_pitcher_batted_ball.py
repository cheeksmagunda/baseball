"""Backfill pitcher batted-ball profile (GB% / FB% / LD% / IFFB%) onto
player_slate.  Phase C add (May 2026).

The cleanest single-number summary of "what does this pitcher's contact
look like" — informs HR risk in different parks (extreme FB% amplifies
risk in Coors / Yankee Stadium short-RF; extreme GB% mutes it).

Source: bulk Statcast season pull (shared parquet).  We aggregate
season-to-date per pitcher_mlb_id from the `bb_type` field.

Usage:
    python scripts/backfill_pitcher_batted_ball.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-pitcher-bb-stub")

from app.core import historical_db  # noqa: E402
from scripts._statcast_bulk import load_bulk_statcast  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_pitcher_batted_ball")

MIN_BATTED_BALLS = 30


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
    if "bb_type" not in df.columns or "pitcher" not in df.columns:
        log.warning("Statcast frame missing bb_type/pitcher columns")
        return 0

    sub = df[["pitcher", "bb_type"]].copy()
    sub = sub.dropna(subset=["pitcher", "bb_type"])
    sub["pitcher"] = sub["pitcher"].astype(int)

    counts = sub.groupby(["pitcher", "bb_type"]).size().unstack(fill_value=0)
    # Total batted balls = GB + FB + LD + Popup (everything in bb_type)
    for col in ("ground_ball", "fly_ball", "line_drive", "popup"):
        if col not in counts.columns:
            counts[col] = 0
    counts["total_bb"] = counts[["ground_ball", "fly_ball", "line_drive", "popup"]].sum(axis=1)
    counts = counts[counts["total_bb"] >= MIN_BATTED_BALLS]

    counts["pitcher_gb_pct"] = counts["ground_ball"] / counts["total_bb"] * 100
    counts["pitcher_fb_pct"] = counts["fly_ball"] / counts["total_bb"] * 100
    counts["pitcher_ld_pct"] = counts["line_drive"] / counts["total_bb"] * 100
    fb_total = counts["fly_ball"] + counts["popup"]
    counts["pitcher_iffb_pct"] = (counts["popup"] / fb_total.where(fb_total > 0, 1)) * 100
    counts.loc[fb_total == 0, "pitcher_iffb_pct"] = None

    lookup = {
        int(idx): {
            "pitcher_gb_pct": round(float(row["pitcher_gb_pct"]), 2),
            "pitcher_fb_pct": round(float(row["pitcher_fb_pct"]), 2),
            "pitcher_ld_pct": round(float(row["pitcher_ld_pct"]), 2),
            "pitcher_iffb_pct": (
                round(float(row["pitcher_iffb_pct"]), 2)
                if pd.notna(row["pitcher_iffb_pct"]) else None
            ),
        }
        for idx, row in counts.iterrows()
    }
    log.info("batted-ball profile computed for %d pitchers", len(lookup))

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE position IN ('P','SP','RP','TWP')"
        else:
            where = (
                "WHERE position IN ('P','SP','RP','TWP') "
                "AND pitcher_gb_pct IS NULL"
            )
        cur = conn.execute(
            f"SELECT slate_date, mlb_id FROM player_slate {where} "
            "ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        log.info("pitcher rows to populate: %d", len(targets))

        updates = 0
        misses = 0
        for t in targets:
            rec = lookup.get(t["mlb_id"])
            if not rec:
                misses += 1
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], rec,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no profile: %d)", updates, misses)
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
