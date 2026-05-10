"""Backfill pitcher velocity trend onto player_slate.

Phase C add (May 2026).  Computes per-(slate_date, mlb_id):
  pitcher_velo_trend_3start = mean_FB_velo_last_3_starts − mean_FB_velo_season

Negative values flag declining velocity, often preceding IL stints or
blow-up starts.  fb_velo (the season aggregate) is already populated;
this script delta-computes the recent-form gap.

Source: bulk Statcast season pull (shared parquet).  We restrict to
4-seam fastballs (pitch_type='FF') for the velocity calculation —
sinker / cutter velos drift on different curves and aren't comparable.

Usage:
    python scripts/backfill_pitcher_velo_trend.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date as DateType
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-velo-trend-stub")

from app.core import historical_db  # noqa: E402
from scripts._statcast_bulk import load_bulk_statcast  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_pitcher_velo_trend")

MIN_STARTS_FOR_TREND = 3


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
    if "release_speed" not in df.columns or "pitcher" not in df.columns:
        log.warning("Statcast frame missing release_speed/pitcher columns")
        return 0

    # Restrict to 4-seam fastballs for comparable velocity reads
    sub = df[df["pitch_type"] == "FF"][["pitcher", "game_date", "release_speed"]].copy()
    sub = sub.dropna(subset=["pitcher", "release_speed"])
    sub["pitcher"] = sub["pitcher"].astype(int)
    sub["game_date"] = pd.to_datetime(sub["game_date"]).dt.date

    # Per (pitcher, game_date): mean FF velocity for that game
    per_game = sub.groupby(["pitcher", "game_date"])["release_speed"].mean().reset_index()
    per_game = per_game.rename(columns={"release_speed": "game_avg_velo"})

    # Index by pitcher
    by_pitcher = {
        int(p): g.sort_values("game_date").reset_index(drop=True)
        for p, g in per_game.groupby("pitcher")
    }
    log.info("velocity per game indexed for %d pitchers", len(by_pitcher))

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE position IN ('P','SP','RP','TWP')"
        else:
            where = (
                "WHERE position IN ('P','SP','RP','TWP') "
                "AND pitcher_velo_trend_3start IS NULL"
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
            slate_d = DateType.fromisoformat(t["slate_date"])
            history = by_pitcher.get(t["mlb_id"])
            if history is None or history.empty:
                misses += 1
                continue
            prior = history[history["game_date"] < slate_d]
            if len(prior) < MIN_STARTS_FOR_TREND:
                misses += 1
                continue
            season_avg = float(prior["game_avg_velo"].mean())
            last_3_avg = float(prior.tail(3)["game_avg_velo"].mean())
            delta = round(last_3_avg - season_avg, 3)
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"],
                {"pitcher_velo_trend_3start": delta},
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (insufficient history: %d)", updates, misses)
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
