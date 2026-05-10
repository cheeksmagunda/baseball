"""Backfill rolling-window per-handedness OPS splits onto player_slate.

Tier 2 D8 of the May 2026 cleanup-and-add sweep.

Schema columns populated:
  ops_vs_lhp_last_20  — OPS-proxy vs LHP, last 20 days
  ops_vs_rhp_last_20  — OPS-proxy vs RHP, last 20 days

Source: Baseball Savant via pybaseball.statcast(start, end) — a single
season-wide bulk pull, then pandas groupby to compute rolling 20-day
splits per (mlb_id, slate_date, p_throws).  This is ~500x faster than
one statcast_batter call per row.

The existing season-aggregate `ops_vs_lhp_at_slate` / `ops_vs_rhp_at_slate`
columns (Step 4 backfill) go stale fast — late-March game samples are
tiny and a 20-30 PA hot streak vs LHP can flip the platoon picture
entirely.  The rolling-window splits give the calibration a more
responsive matchup signal.

OPS proxy: 1.5 × mean(estimated_woba_using_speedangle) over batted-ball
events in the window.  This is the same per-PA xwOBA → OPS conversion
the live runtime uses for short-window matchup signals.

Cache: scripts/output/.recent_handedness_cache/<season>_bulk.parquet —
single file per season caching the bulk Statcast pull.

Usage:
    python scripts/backfill_recent_handedness_splits.py
    python scripts/backfill_recent_handedness_splits.py --window 20
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date as DateType, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-recent-handedness-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".recent_handedness_cache"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_recent_handedness_splits")


def _bulk_fetch_season(start_date: str, end_date: str):
    """One Statcast pull for the entire season.  Cached as parquet."""
    try:
        import pandas as pd
        from pybaseball import statcast
    except ImportError:
        log.warning("pybaseball/pandas not installed")
        return None

    cache_file = CACHE_DIR / f"statcast_{start_date}_{end_date}.parquet"
    if cache_file.exists():
        try:
            df = pd.read_parquet(cache_file)
            log.info("loaded cached bulk statcast: %d events", len(df))
            return df
        except Exception as e:
            log.warning("cache read failed: %s", e)

    log.info("bulk fetching statcast %s → %s (this can take 1-2 minutes)…", start_date, end_date)
    try:
        df = statcast(start_dt=start_date, end_dt=end_date, verbose=False)
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", type=int, default=20, help="Days back from slate.")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    try:
        import pandas as pd
    except ImportError:
        log.error("pandas required")
        return 1

    season_start = f"{args.season}-03-01"
    season_end = f"{args.season}-11-15"
    df = _bulk_fetch_season(season_start, season_end)
    if df is None or df.empty:
        log.warning(
            "0 rows written — bulk Statcast pull empty/unreachable.  "
            "Schema is in place; re-run when reachable."
        )
        return 0

    # Pick the woba estimate column
    woba_col = (
        "estimated_woba_using_speedangle"
        if "estimated_woba_using_speedangle" in df.columns
        else "woba_value"
    )
    if woba_col not in df.columns:
        log.warning("no wOBA estimate column on Statcast frame")
        return 0

    # Keep just rows we'll use
    df = df[["game_date", "batter", "p_throws", woba_col]].copy()
    df = df[df[woba_col].notna()]
    df["game_date"] = pd.to_datetime(df["game_date"]).dt.date

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE position NOT IN ('P','SP','RP','TWP')"
        else:
            where = (
                "WHERE position NOT IN ('P','SP','RP','TWP') "
                "AND ops_vs_lhp_last_20 IS NULL AND ops_vs_rhp_last_20 IS NULL"
            )
        cur = conn.execute(
            f"SELECT slate_date, mlb_id FROM player_slate {where} "
            "ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        log.info("batter rows to populate: %d", len(targets))

        # Index events by batter for fast lookup
        events_by_batter = {
            mlb_id: g
            for mlb_id, g in df.groupby("batter", sort=False)
        }

        updates = 0
        misses = 0
        for t in targets:
            slate_d = DateType.fromisoformat(t["slate_date"])
            start = slate_d - timedelta(days=args.window)
            end = slate_d - timedelta(days=1)
            ev = events_by_batter.get(t["mlb_id"])
            if ev is None:
                misses += 1
                continue
            window = ev[(ev["game_date"] >= start) & (ev["game_date"] <= end)]
            if window.empty:
                misses += 1
                continue
            rec: dict = {}
            for hand_code, col in (("L", "ops_vs_lhp_last_20"), ("R", "ops_vs_rhp_last_20")):
                sub = window[window["p_throws"] == hand_code]
                if sub.empty:
                    continue
                # 1.5 × mean(xwOBA) ≈ OPS at the per-PA level
                rec[col] = round(float(sub[woba_col].mean()) * 1.5, 4)
            if not rec:
                misses += 1
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], rec,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no recent splits: %d)", updates, misses)
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
