"""Backfill `player_slate.pitcher_rest_days` from `player_game_log`.

Tier 1 D3 of the May 2026 cleanup-and-add sweep.

For each pitcher row in player_slate, find the most recent prior game
where the same mlb_id had IP > 0 and write the day delta as
`pitcher_rest_days`.  If no prior IP > 0 game exists in the corpus, leave
NULL (true season-debut starter or thin-sample spot starter — the rookie
admission rule handles those at scoring time).

Pure derivation — no external calls.  Runs in seconds against the full
corpus.

Usage:
    python scripts/backfill_pitcher_rest.py
    python scripts/backfill_pitcher_rest.py --force
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-pitcher-rest-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_pitcher_rest")


PITCHER_POSITIONS = {"P", "SP", "RP", "TWP"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="Recompute even rows where pitcher_rest_days is set.")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # Load every (mlb_id, game_date) where the pitcher logged outs.
        # Use IP > 0 as the gate; players who PA'd without pitching don't
        # count toward pitcher rest.
        cur = conn.execute(
            "SELECT mlb_id, game_date FROM player_game_log "
            "WHERE ip IS NOT NULL AND ip > 0 "
            "ORDER BY mlb_id, game_date"
        )
        appearance_index: dict[int, list[str]] = {}
        for r in cur.fetchall():
            appearance_index.setdefault(r["mlb_id"], []).append(r["game_date"])

        # Pick targets: pitcher rows in player_slate.  When --force is off,
        # skip rows that already have pitcher_rest_days set.
        if args.force:
            where = "WHERE position IN ('P','SP','RP','TWP')"
        else:
            where = (
                "WHERE position IN ('P','SP','RP','TWP') "
                "AND pitcher_rest_days IS NULL"
            )
        cur = conn.execute(
            f"SELECT slate_date, mlb_id FROM player_slate {where} "
            "ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        log.info("pitcher rows to populate: %d", len(targets))

        updates = 0
        skipped = 0
        for t in targets:
            slate_d = DateType.fromisoformat(t["slate_date"])
            history = appearance_index.get(t["mlb_id"], [])
            # Find the latest game_date < slate_date.
            prior = [g for g in history if g < t["slate_date"]]
            if not prior:
                skipped += 1
                continue
            last_game = DateType.fromisoformat(prior[-1])
            rest_days = (slate_d - last_game).days
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"],
                {"pitcher_rest_days": rest_days},
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no prior appearance: %d)", updates, skipped)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        # Phase-3-hook export refresh, gated as in other backfills.
        if not os.environ.get("HISTORICAL_DB"):
            from scripts.export_historical_csvs import export_all
            export_all()
    sys.exit(rc)
