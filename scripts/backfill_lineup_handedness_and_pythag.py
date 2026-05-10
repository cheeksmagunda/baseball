"""Backfill lineup handedness composition + pythagorean gap onto slate_game.

Phase C add (May 2026).  Two derived signals.

(1) Lineup handedness counts: how many LHB / RHB / switch in each
    starting lineup.  A 7-LHB lineup vs an LHP starter is a meaningful
    matchup-level signal beyond what individual platoon splits capture
    (the LHP can pace himself differently when 7 of 9 bats are lefty).

(2) Pythagorean gap: actual W-L% minus pythag W-L% from runs scored
    and allowed.  Sign-flips the regression direction:
      gap > 0  → team is "lucky" (winning more than runs justify), due
                 for negative regression
      gap < 0  → team is "unlucky", due for positive regression

All inputs are already in the DB:
  - player_dim.bat_side + player_slate.batting_order_at_slate
  - slate_game.home_team_runs_scored / runs_allowed / home_record / away_record

Free, deterministic, no network.

Usage:
    python scripts/backfill_lineup_handedness_and_pythag.py
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-lineup-pythag-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_lineup_pythag")


def _parse_record(rec: str | None) -> tuple[int, int]:
    """Parse 'W-L' string into (W, L).  Returns (0, 0) if unparseable."""
    if not rec or "-" not in rec:
        return 0, 0
    try:
        wins, losses = rec.split("-", 1)
        return int(wins), int(losses)
    except (ValueError, TypeError):
        return 0, 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # 1) Lineup handedness — per (slate_date, team), count distinct
        # mlb_ids where batting_order ≤ 9 and join to player_dim.bat_side.
        cur = conn.execute("SELECT mlb_id, bat_side FROM player_dim WHERE bat_side IS NOT NULL")
        bat_side: dict[int, str] = {r["mlb_id"]: r["bat_side"] for r in cur.fetchall()}
        log.info("bat_side lookup: %d players", len(bat_side))

        cur = conn.execute(
            "SELECT slate_date, team, mlb_id, batting_order_at_slate "
            "FROM player_slate "
            "WHERE batting_order_at_slate IS NOT NULL "
            "AND batting_order_at_slate <= 9 "
            "AND batting_order_at_slate >= 1"
        )
        # {(slate_date, team) -> {L: n, R: n, S: n}}
        composition: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {"L": 0, "R": 0, "S": 0}
        )
        for r in cur.fetchall():
            side = bat_side.get(r["mlb_id"])
            if not side:
                continue
            composition[(r["slate_date"], r["team"])][side] += 1
        log.info("lineup compositions: %d (slate_date, team) pairs", len(composition))

        # 2) Pythag gap — slate_game has runs_scored / runs_allowed and the
        # home/away_record split.  Sum W and L across home + away records to
        # get the team's actual record on slate_date.
        if args.force:
            where = "WHERE 1=1"
        else:
            where = (
                "WHERE (home_lineup_lhb_count IS NULL OR home_pythag_gap IS NULL)"
            )
        cur = conn.execute(
            f"SELECT slate_date, game_pk, home_team, away_team, "
            f"home_team_runs_scored, home_team_runs_allowed, "
            f"home_team_home_record, home_team_away_record, "
            f"away_team_runs_scored, away_team_runs_allowed, "
            f"away_team_home_record, away_team_away_record "
            f"FROM slate_game {where}"
        )
        targets = cur.fetchall()
        log.info("targets: %d games", len(targets))

        updates = 0
        for t in targets:
            updates_dict: dict = {}

            # Lineup composition
            for side, team in (("home", t["home_team"]), ("away", t["away_team"])):
                comp = composition.get((t["slate_date"], team), {"L": 0, "R": 0, "S": 0})
                updates_dict[f"{side}_lineup_lhb_count"] = comp["L"]
                updates_dict[f"{side}_lineup_rhb_count"] = comp["R"]
                updates_dict[f"{side}_lineup_switch_count"] = comp["S"]

            # Pythagorean gap
            for side in ("home", "away"):
                rs = t[f"{side}_team_runs_scored"]
                ra = t[f"{side}_team_runs_allowed"]
                hw, hl = _parse_record(t[f"{side}_team_home_record"])
                aw, al = _parse_record(t[f"{side}_team_away_record"])
                wins = hw + aw
                losses = hl + al
                if rs is not None and ra is not None and (wins + losses) > 0:
                    if (rs * rs + ra * ra) > 0:
                        pythag = (rs * rs) / (rs * rs + ra * ra)
                    else:
                        continue
                    actual = wins / (wins + losses)
                    updates_dict[f"{side}_pythag_gap"] = round(actual - pythag, 4)

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
