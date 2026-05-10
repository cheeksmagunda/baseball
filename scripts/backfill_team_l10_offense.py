"""Backfill team L10 wOBA + RPG (recent offensive momentum) onto slate_game.

Phase E add (May 2026 batter sweep).  L10-wins ≠ runs scored — a team
can be 7-3 winning 3-2 every night while the offense is dead.  L10
wOBA + RPG capture run-environment trend orthogonal to wins.

Source: bulk Statcast pull (shared parquet).  We aggregate per
(team, game_date) → (wOBA, runs) then take the rolling-10-game window
ending on slate_date for each (slate_date, team) pair.

Usage:
    python scripts/backfill_team_l10_offense.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date as DateType
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-team-l10-stub")

from app.core import historical_db  # noqa: E402
from scripts._statcast_bulk import load_bulk_statcast  # noqa: E402

# Statcast team abbreviations differ from our canonical 3-letter codes
# in a handful of edge cases — we resolve via slate_game home/away_team
# matching directly rather than depending on cross-source mapping.

WINDOW_GAMES = 10

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_team_l10_offense")


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

    # We need: per (game_date, team), runs scored + woba.
    # Statcast rows are per-pitch.  Inning_topbot determines which side is
    # batting; home/away_team in the row tells us the matchup.  Sum
    # (post_bat_score - bat_score) per AB for runs.  For wOBA, mean
    # estimated_woba over PA-ending pitches.
    needed = ["game_date", "home_team", "away_team", "inning_topbot",
              "events", "post_bat_score", "bat_score", "estimated_woba_using_speedangle"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        log.warning("Statcast frame missing %s", missing)
        return 0
    sub = df[needed].copy()
    sub["game_date"] = pd.to_datetime(sub["game_date"]).dt.date.astype(str)
    # Batting team per row
    sub["bat_team"] = sub.apply(
        lambda r: r["away_team"] if r["inning_topbot"] == "Top" else r["home_team"],
        axis=1,
    )
    # Runs scored on the row
    sub["runs"] = (sub["post_bat_score"] - sub["bat_score"]).fillna(0)
    # PA end markers
    sub["is_pa_end"] = sub["events"].notna() & (sub["events"] != "")

    # Aggregate per (game_date, team)
    agg = sub.groupby(["game_date", "bat_team"]).agg(
        runs=("runs", "sum"),
        pa_count=("is_pa_end", "sum"),
        woba_sum=(
            "estimated_woba_using_speedangle",
            lambda s: s.dropna().sum(),
        ),
        woba_n=(
            "estimated_woba_using_speedangle",
            lambda s: s.dropna().count(),
        ),
    ).reset_index()
    agg = agg.rename(columns={"bat_team": "team"})

    # Build per-team list of {date, runs, woba_sum, woba_n} sorted by date
    by_team: dict[str, list[dict]] = defaultdict(list)
    for _, r in agg.iterrows():
        by_team[r["team"]].append({
            "date": r["game_date"],
            "runs": int(r["runs"]),
            "woba_sum": float(r["woba_sum"]),
            "woba_n": int(r["woba_n"]),
        })
    for team in by_team:
        by_team[team].sort(key=lambda x: x["date"])
    log.info("team-game offensive aggregates: %d teams", len(by_team))

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE 1=1"
        else:
            where = "WHERE home_team_woba_l10 IS NULL OR away_team_woba_l10 IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, game_pk, home_team, away_team FROM slate_game {where}"
        )
        targets = cur.fetchall()
        log.info("targets: %d games", len(targets))

        updates = 0
        for t in targets:
            slate_d = DateType.fromisoformat(t["slate_date"]).isoformat()
            updates_dict: dict = {}
            for side, team in (("home", t["home_team"]), ("away", t["away_team"])):
                history = by_team.get(team, [])
                prior = [h for h in history if h["date"] < slate_d]
                window = prior[-WINDOW_GAMES:]
                if not window:
                    continue
                runs = sum(h["runs"] for h in window)
                woba_sum = sum(h["woba_sum"] for h in window)
                woba_n = sum(h["woba_n"] for h in window)
                rpg = runs / len(window)
                woba = (woba_sum / woba_n) if woba_n > 0 else None
                updates_dict[f"{side}_team_rpg_l10"] = round(rpg, 3)
                if woba is not None:
                    updates_dict[f"{side}_team_woba_l10"] = round(woba, 4)
            if updates_dict:
                historical_db.update_slate_game_columns(
                    conn, t["slate_date"], t["game_pk"], updates_dict,
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
