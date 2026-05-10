"""Backfill all BABIP/HR-FB features onto player_slate (batter rows).

Consolidated May 2026 — merges:
    - backfill_regression_flags.py  (babip_at_slate / hr_fb_at_slate +
                                      regression flags vs league norm)
    - backfill_babip_delta.py       (rolling 30-day BABIP minus
                                      season-aggregate BABIP)

All four columns share `player_game_log` (BABIP) + bulk Statcast
(HR/FB launch_angle≥25°) as their source — combining into one script
eliminates duplicate index-building over the same data.

Columns populated (batters only):
  babip_at_slate          — rolling 30-day BABIP from player_game_log
  babip_regression_flag   — 1 if BABIP > league-avg + LUCK_DELTA
  hr_fb_at_slate          — rolling 30-day HR / fly_balls from Statcast
  hr_fb_regression_flag   — 1 if HR/FB > league-avg + LUCK_DELTA
  babip_delta_30day       — rolling 30-day BABIP minus full-history BABIP

Usage:
    python scripts/backfill_babip_features.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import date as DateType, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts._backfill_common import bootstrap, finalize  # noqa: E402

bootstrap("backfill-babip-stub")

from app.core import historical_db  # noqa: E402
from scripts._statcast_bulk import load_bulk_statcast  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_babip_features")

WINDOW_DAYS = 30
LEAGUE_BABIP = 0.295
BABIP_LUCK_DELTA = 0.05
LEAGUE_HR_FB = 0.13
HR_FB_LUCK_DELTA = 0.04


def _babip(rows: list[dict]) -> float | None:
    ab = sum(r["ab"] for r in rows)
    hits = sum(r["hits"] for r in rows)
    hr = sum(r["hr"] for r in rows)
    so = sum(r["so"] for r in rows)
    denom = ab - so - hr  # sf treated as 0; player_game_log has no sf
    if denom <= 0:
        return None
    return (hits - hr) / denom


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    try:
        import pandas as pd
    except ImportError:
        pd = None

    # 1) Index per_player BABIP-input rows from player_game_log
    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        cur = conn.execute(
            "SELECT slate_date, mlb_id, ab, hits, hr, so FROM player_game_log "
            "WHERE ab IS NOT NULL AND ab > 0"
        )
        per_player: dict[int, list[dict]] = defaultdict(list)
        for r in cur.fetchall():
            per_player[r["mlb_id"]].append({
                "date": r["slate_date"],
                "ab": r["ab"] or 0,
                "hits": r["hits"] or 0,
                "hr": r["hr"] or 0,
                "so": r["so"] or 0,
            })
        for mid in per_player:
            per_player[mid].sort(key=lambda x: x["date"])

        # 2) Index per_player HR/FB-input rows from bulk Statcast
        hr_fb_by_player: dict[int, list[dict]] = defaultdict(list)
        if pd is not None:
            statcast_df = load_bulk_statcast(season=args.season)
            if statcast_df is not None and "launch_angle" in statcast_df.columns:
                sub = statcast_df[["batter", "game_date", "launch_angle", "events"]].copy()
                sub["game_date"] = pd.to_datetime(sub["game_date"]).dt.date.astype(str)
                sub["is_fly_ball"] = sub["launch_angle"].fillna(-999) >= 25
                sub["is_hr"] = sub["events"].fillna("") == "home_run"
                agg = sub.groupby(["batter", "game_date"]).agg(
                    fb=("is_fly_ball", "sum"),
                    hr=("is_hr", "sum"),
                ).reset_index()
                for _, row in agg.iterrows():
                    hr_fb_by_player[int(row["batter"])].append({
                        "date": row["game_date"],
                        "fb": int(row["fb"]),
                        "hr": int(row["hr"]),
                    })
                for mid in hr_fb_by_player:
                    hr_fb_by_player[mid].sort(key=lambda x: x["date"])
                log.info("HR/FB available for %d batters", len(hr_fb_by_player))

        # 3) Iterate batter targets
        if args.force:
            where = "WHERE position NOT IN ('P','SP','RP','TWP')"
        else:
            where = (
                "WHERE position NOT IN ('P','SP','RP','TWP') "
                "AND (babip_at_slate IS NULL OR babip_delta_30day IS NULL "
                "OR hr_fb_at_slate IS NULL)"
            )
        cur = conn.execute(
            f"SELECT slate_date, mlb_id FROM player_slate {where}"
        )
        targets = cur.fetchall()
        log.info("batter rows to populate: %d", len(targets))

        updates = 0
        for t in targets:
            slate_d = DateType.fromisoformat(t["slate_date"])
            cutoff_30 = (slate_d - timedelta(days=WINDOW_DAYS)).isoformat()
            updates_dict: dict = {}

            # BABIP — 30-day window + season-aggregate (delta)
            history = per_player.get(t["mlb_id"], [])
            prior_all = [h for h in history if h["date"] < t["slate_date"]]
            window_30 = [h for h in prior_all if h["date"] >= cutoff_30]
            if window_30:
                babip_30 = _babip(window_30)
                if babip_30 is not None:
                    updates_dict["babip_at_slate"] = round(babip_30, 4)
                    updates_dict["babip_regression_flag"] = int(
                        babip_30 - LEAGUE_BABIP > BABIP_LUCK_DELTA
                    )
                if prior_all:
                    babip_season = _babip(prior_all)
                    if babip_season is not None and babip_30 is not None:
                        updates_dict["babip_delta_30day"] = round(babip_30 - babip_season, 4)

            # HR/FB — 30-day window from Statcast
            sc_history = hr_fb_by_player.get(t["mlb_id"], [])
            sc_window = [h for h in sc_history if cutoff_30 <= h["date"] < t["slate_date"]]
            if sc_window:
                fb = sum(h["fb"] for h in sc_window)
                hr_sc = sum(h["hr"] for h in sc_window)
                if fb > 0:
                    hr_fb = hr_sc / fb
                    updates_dict["hr_fb_at_slate"] = round(hr_fb, 4)
                    updates_dict["hr_fb_regression_flag"] = int(
                        hr_fb - LEAGUE_HR_FB > HR_FB_LUCK_DELTA
                    )

            if updates_dict:
                historical_db.update_player_slate_columns(
                    conn, t["slate_date"], t["mlb_id"], updates_dict,
                )
                updates += 1
        conn.commit()
        log.info("UPDATE rows: %d", updates)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(finalize(main()))
