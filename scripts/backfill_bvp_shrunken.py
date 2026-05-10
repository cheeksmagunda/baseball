"""Backfill batter-vs-pitcher (BvP) wOBA with Bayesian shrinkage.

Phase E add (May 2026 batter sweep).

Raw BvP samples are 5-15 PAs and noisy.  Shrinking toward the league
batter rate makes BvP usable as a calibration signal:
  shrunken_wOBA = (n × bvp_wOBA + α × LEAGUE_AVG) / (n + α)

Where α is the shrinkage prior strength (PAs of league prior).  Set
α = 50 — empirically the sweet spot for MLB DFS data: 5-PA samples
get 90% league-prior; 50-PA samples get 50/50; 200+ PAs get 80%
batter-specific.

Source: bulk Statcast pull (shared parquet).  For each (slate_date,
batter) row we look at the batter's PRIOR-to-slate-date PAs against
the slate's opposing starter.  Stored on player_slate (batter rows
only) — the OPPOSING starter is identified via slate_game.{home,away}_starter_id.

Usage:
    python scripts/backfill_bvp_shrunken.py
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-bvp-stub")

from app.core import historical_db  # noqa: E402
from scripts._statcast_bulk import load_bulk_statcast  # noqa: E402

LEAGUE_AVG_WOBA = 0.320
SHRINKAGE_ALPHA = 50

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_bvp_shrunken")


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

    woba_col = (
        "estimated_woba_using_speedangle"
        if "estimated_woba_using_speedangle" in df.columns
        else "woba_value"
    )
    needed = ["batter", "pitcher", "game_date", woba_col]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        log.warning("Statcast frame missing %s", missing)
        return 0

    sub = df[needed].copy()
    sub = sub.dropna(subset=needed)
    sub["batter"] = sub["batter"].astype(int)
    sub["pitcher"] = sub["pitcher"].astype(int)
    sub["game_date"] = pd.to_datetime(sub["game_date"]).dt.date.astype(str)

    # Per (batter, pitcher, game_date): aggregate PA-level wOBA (mean over
    # the PA's batted-ball events, but here we're just averaging per-pitch
    # which is close enough for shrunken BvP).  Cleaner: count PAs by
    # grouping at_bat_number too if present.
    if "at_bat_number" in df.columns:
        sub2 = df[["batter", "pitcher", "game_date", "at_bat_number", woba_col]].copy()
        sub2 = sub2.dropna(subset=["batter", "pitcher", "game_date", "at_bat_number", woba_col])
        sub2["batter"] = sub2["batter"].astype(int)
        sub2["pitcher"] = sub2["pitcher"].astype(int)
        sub2["game_date"] = pd.to_datetime(sub2["game_date"]).dt.date.astype(str)
        # Per PA: take last pitch's wOBA
        per_pa = sub2.sort_values("at_bat_number").groupby(
            ["batter", "pitcher", "game_date", "at_bat_number"]
        )[woba_col].last().reset_index()
    else:
        per_pa = sub  # fallback approximation

    # Index: {(batter, pitcher) -> sorted list[(date, woba)]}
    by_pair: dict[tuple[int, int], list[tuple[str, float]]] = defaultdict(list)
    for _, r in per_pa.iterrows():
        by_pair[(int(r["batter"]), int(r["pitcher"]))].append(
            (r["game_date"], float(r[woba_col]))
        )
    for pair in by_pair:
        by_pair[pair].sort(key=lambda x: x[0])
    log.info("BvP pair PAs indexed for %d (batter, pitcher) pairs", len(by_pair))

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # For each batter player_slate row, identify the opposing starter
        # from slate_game.{home,away}_starter_id (whichever is NOT the
        # batter's own team).
        if args.force:
            where = "WHERE ps.position NOT IN ('P','SP','RP','TWP')"
        else:
            where = (
                "WHERE ps.position NOT IN ('P','SP','RP','TWP') "
                "AND ps.bvp_woba_vs_starter IS NULL"
            )
        cur = conn.execute(
            f"""
            SELECT ps.slate_date, ps.mlb_id, ps.team, ps.game_pk,
                   sg.home_team, sg.away_team,
                   sg.home_starter_id, sg.away_starter_id
            FROM player_slate ps
            LEFT JOIN slate_game sg
              ON sg.slate_date = ps.slate_date AND sg.game_pk = ps.game_pk
            {where}
            """
        )
        targets = cur.fetchall()
        log.info("batter rows to populate: %d", len(targets))

        updates = 0
        misses = 0
        for t in targets:
            opposing_starter = (
                t["away_starter_id"] if t["team"] == t["home_team"]
                else t["home_starter_id"]
            )
            if not opposing_starter:
                misses += 1
                continue
            history = by_pair.get((t["mlb_id"], int(opposing_starter)), [])
            prior = [w for d, w in history if d < t["slate_date"]]
            n = len(prior)
            if n == 0:
                # Still write n=0 + shrunken=league_avg (so we know we
                # checked).  Useful for calibration.
                rec = {
                    "bvp_pa_count_vs_starter": 0,
                    "bvp_woba_vs_starter": LEAGUE_AVG_WOBA,
                }
            else:
                bvp_woba = sum(prior) / n
                shrunken = (n * bvp_woba + SHRINKAGE_ALPHA * LEAGUE_AVG_WOBA) / (n + SHRINKAGE_ALPHA)
                rec = {
                    "bvp_pa_count_vs_starter": n,
                    "bvp_woba_vs_starter": round(shrunken, 4),
                }
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], rec,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no opposing starter: %d)", updates, misses)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0 and not os.environ.get("HISTORICAL_DB"):
        from scripts.export_historical_csvs import export_all
        export_all()
    sys.exit(rc)
