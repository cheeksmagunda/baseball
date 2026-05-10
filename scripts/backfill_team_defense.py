"""Backfill team defensive runs saved (DRS proxy) onto slate_game.

Phase C add (May 2026).  We derive team defense by summing the
`outs_above_avg` (Statcast OAA) values for each team's roster, taken
from player_slate (already populated by backfill_sprint_oaa.py).

Why "DRS proxy" not actual DRS:
  - DRS (Baseball Info Solutions / FanGraphs) and OAA (Statcast) are
    correlated at ~0.85 but methodologically different.  OAA only
    measures range; DRS includes range + arm + double-play + scoring
    decisions.  For our calibration purpose ("does the team behind this
    pitcher suppress BABIP?") OAA is the cleaner of the two and free.
  - Multiplying OAA by ~0.8 gives a runs-saved estimate (industry
    standard conversion: 1 OAA ≈ 0.8 runs).  We store the runs estimate
    in home_team_defense_drs / away_team_defense_drs.

Free, deterministic, no network.

Usage:
    python scripts/backfill_team_defense.py
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-team-defense-stub")

from app.core import historical_db  # noqa: E402

OAA_TO_RUNS = 0.8

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_team_defense")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # Sum OAA per team across the entire corpus (season aggregate, the
        # number is updated to season-to-date by the next OAA backfill run).
        cur = conn.execute(
            "SELECT team, SUM(outs_above_avg) AS oaa "
            "FROM player_slate "
            "WHERE outs_above_avg IS NOT NULL "
            "GROUP BY team"
        )
        # Note: this aggregates across slate-dates so a player's OAA gets
        # counted N times (once per slate the player appears).  We need to
        # de-duplicate to one row per (team, mlb_id) — take the latest OAA.
        cur = conn.execute(
            """
            SELECT team, mlb_id, outs_above_avg
            FROM player_slate ps
            WHERE outs_above_avg IS NOT NULL
            AND slate_date = (
                SELECT MAX(slate_date) FROM player_slate ps2
                WHERE ps2.mlb_id = ps.mlb_id
                AND ps2.outs_above_avg IS NOT NULL
            )
            """
        )
        team_oaa: dict[str, float] = defaultdict(float)
        for r in cur.fetchall():
            team_oaa[r["team"]] += float(r["outs_above_avg"])
        log.info("team OAA aggregates: %d teams", len(team_oaa))

        # Convert to runs and write to slate_game
        if args.force:
            where = "WHERE 1=1"
        else:
            where = "WHERE home_team_defense_drs IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, game_pk, home_team, away_team FROM slate_game {where}"
        )
        targets = cur.fetchall()
        log.info("targets: %d games", len(targets))

        updates = 0
        for t in targets:
            updates_dict = {
                "home_team_defense_drs": round(
                    team_oaa.get(t["home_team"], 0.0) * OAA_TO_RUNS, 2
                ),
                "away_team_defense_drs": round(
                    team_oaa.get(t["away_team"], 0.0) * OAA_TO_RUNS, 2
                ),
            }
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
    if rc == 0:
        if not os.environ.get("HISTORICAL_DB"):
            from scripts.export_historical_csvs import export_all
            export_all()
    sys.exit(rc)
