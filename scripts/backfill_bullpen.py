"""Backfill all bullpen-related slate_game columns.

Consolidated May 2026 — merges three previous scripts:
    - backfill_bullpen_rest.py        (2d/3d total pitch counts)
    - backfill_bullpen_handedness.py  (2d L/R pitch counts)
    - backfill_bullpen_composition.py (14d distinct L/R reliever counts)

Why merge: the old `bullpen_rest` derived totals from
`slate_game.{home,away}_bullpen_pitch_count` (only slate dates),
while `bullpen_handedness` derived L/R from `player_game_log` (all
games).  The two sources disagreed on 14/551 games.  This unified
script uses ONE source — `player_game_log` — so totals are the L+R
sum exactly, and the same loop produces all four windows.

All eight slate_game columns are populated:
    home_bullpen_2d_pitches            ← L_2d + R_2d
    home_bullpen_3d_pitches            ← L_3d + R_3d
    home_bullpen_lhp_pitches_2d
    home_bullpen_rhp_pitches_2d
    home_bullpen_lhp_count             ← distinct LHP relievers, 14d
    home_bullpen_rhp_count             ← distinct RHP relievers, 14d
    (× 2 for home / away)

Pitch-count proxy: `ip * 15` (league-average pitches/inning).  Acceptable
approximation — for predictive purposes the relative L/R / 2d/3d shape
matters more than the absolute count.

Usage:
    python scripts/backfill_bullpen.py
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

bootstrap("backfill-bullpen-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_bullpen")

PITCHES_PER_INNING_PROXY = 15
COMPOSITION_WINDOW_DAYS = 14


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # 1) pitch_hand lookup
        cur = conn.execute(
            "SELECT mlb_id, pitch_hand FROM player_dim WHERE pitch_hand IS NOT NULL"
        )
        pitch_hand: dict[int, str] = {r["mlb_id"]: r["pitch_hand"] for r in cur.fetchall()}

        # 2) Set of (game_date, mlb_id) where pitcher started — to exclude
        # starters from the bullpen aggregate.
        cur = conn.execute(
            "SELECT slate_date, home_starter_id, away_starter_id FROM slate_game"
        )
        started_on: set[tuple[str, int]] = set()
        for r in cur.fetchall():
            for sid in (r["home_starter_id"], r["away_starter_id"]):
                if sid:
                    started_on.add((r["slate_date"], int(sid)))

        # 3) Pull all (game_date, mlb_id, team, ip) tuples for non-starters.
        cur = conn.execute(
            "SELECT slate_date, mlb_id, team, ip FROM player_game_log "
            "WHERE ip IS NOT NULL AND ip > 0"
        )
        # team_appearances[team] = list[(date, mlb_id, hand, est_pitches)]
        team_appearances: dict[str, list[tuple[str, int, str, int]]] = defaultdict(list)
        for r in cur.fetchall():
            mid = int(r["mlb_id"])
            if (r["slate_date"], mid) in started_on:
                continue
            hand = pitch_hand.get(mid)
            if not hand:
                continue
            est_pitches = int(round(float(r["ip"]) * PITCHES_PER_INNING_PROXY))
            team_appearances[r["team"]].append(
                (r["slate_date"], mid, hand, est_pitches)
            )
        log.info(
            "non-starter appearances by team: %d teams, %d total appearances",
            len(team_appearances),
            sum(len(v) for v in team_appearances.values()),
        )

        # 4) For each slate_game, compute all 4 metrics × 2 sides.
        if args.force:
            where = "WHERE 1=1"
        else:
            where = (
                "WHERE home_bullpen_2d_pitches IS NULL "
                "OR home_bullpen_lhp_pitches_2d IS NULL "
                "OR home_bullpen_lhp_count IS NULL"
            )
        cur = conn.execute(
            f"SELECT slate_date, game_pk, home_team, away_team FROM slate_game {where}"
        )
        targets = cur.fetchall()
        log.info("targets: %d games", len(targets))

        updates = 0
        for t in targets:
            slate_d = DateType.fromisoformat(t["slate_date"])
            cutoff_2d = (slate_d - timedelta(days=2)).isoformat()
            cutoff_3d = (slate_d - timedelta(days=3)).isoformat()
            cutoff_14d = (slate_d - timedelta(days=COMPOSITION_WINDOW_DAYS)).isoformat()
            cutoff_excl = t["slate_date"]

            updates_dict: dict = {}
            for side, team in (("home", t["home_team"]), ("away", t["away_team"])):
                lhp_2d = rhp_2d = 0
                lhp_3d = rhp_3d = 0
                lhp_ids_14d: set[int] = set()
                rhp_ids_14d: set[int] = set()
                for d, mid, hand, pitches in team_appearances.get(team, []):
                    if d >= cutoff_excl:
                        continue
                    if d >= cutoff_14d:
                        if hand == "L":
                            lhp_ids_14d.add(mid)
                        elif hand == "R":
                            rhp_ids_14d.add(mid)
                    if d >= cutoff_3d:
                        if hand == "L":
                            lhp_3d += pitches
                        elif hand == "R":
                            rhp_3d += pitches
                    if d >= cutoff_2d:
                        if hand == "L":
                            lhp_2d += pitches
                        elif hand == "R":
                            rhp_2d += pitches
                updates_dict[f"{side}_bullpen_lhp_pitches_2d"] = lhp_2d
                updates_dict[f"{side}_bullpen_rhp_pitches_2d"] = rhp_2d
                updates_dict[f"{side}_bullpen_2d_pitches"] = lhp_2d + rhp_2d
                updates_dict[f"{side}_bullpen_3d_pitches"] = lhp_3d + rhp_3d
                updates_dict[f"{side}_bullpen_lhp_count"] = len(lhp_ids_14d)
                updates_dict[f"{side}_bullpen_rhp_count"] = len(rhp_ids_14d)
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
    import sys
    sys.exit(finalize(main()))
