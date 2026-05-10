"""Backfill rolling bullpen pitch-count totals onto slate_game.

Tier 2 D7 of the May 2026 cleanup-and-add sweep.

For each (slate_date, game_pk) we sum the per-game `bullpen_pitch_count`
across the trailing 2-day and 3-day windows, per team.  Reads the existing
slate_game.{home,away}_bullpen_pitch_count columns (Step-10 backfill).

Pure derivation — no external calls.

The per-team aggregation needs a team identifier; we walk every slate
the team played in over the trailing window, including non-corpus
slates if the slate_game row is present.

Usage:
    python scripts/backfill_bullpen_rest.py
    python scripts/backfill_bullpen_rest.py --force
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date as DateType, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-bullpen-rest-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_bullpen_rest")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # Build per-team-per-date pitch-count history (sum home + away
        # contributions if the team appeared on both sides of doubleheaders).
        cur = conn.execute(
            "SELECT slate_date, home_team, away_team, "
            "       home_bullpen_pitch_count, away_bullpen_pitch_count "
            "FROM slate_game"
        )
        history: dict[tuple[str, str], int] = defaultdict(int)
        for r in cur.fetchall():
            for team_key, pitch_key in (
                ("home_team", "home_bullpen_pitch_count"),
                ("away_team", "away_bullpen_pitch_count"),
            ):
                team = r[team_key]
                pitches = r[pitch_key]
                if team is None or pitches is None:
                    continue
                history[(r["slate_date"], team)] += int(pitches)

        if args.force:
            where = "WHERE 1=1"
        else:
            where = "WHERE home_bullpen_2d_pitches IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, game_pk, game_number, home_team, away_team "
            f"FROM slate_game {where} ORDER BY slate_date, game_pk"
        )
        targets = cur.fetchall()
        log.info("game rows to populate: %d", len(targets))

        updates = 0
        for t in targets:
            slate_d = DateType.fromisoformat(t["slate_date"])
            updates_dict: dict = {}
            for side, team in (("home", t["home_team"]), ("away", t["away_team"])):
                p2 = 0
                p3 = 0
                for delta_days in range(1, 4):
                    d = (slate_d - timedelta(days=delta_days)).isoformat()
                    pitches = history.get((d, team), 0)
                    if delta_days <= 2:
                        p2 += pitches
                    p3 += pitches
                updates_dict[f"{side}_bullpen_2d_pitches"] = p2
                updates_dict[f"{side}_bullpen_3d_pitches"] = p3
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
