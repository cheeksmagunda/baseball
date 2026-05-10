"""Backfill is_fresh_off_il flag onto player_slate.

Phase E add (May 2026 batter sweep).

is_fresh_off_il = 1 when the player has an IL-return transaction
(typeCode in {RTN, STA, SE} returning to active) within 7 days prior
to slate_date.  Source: label_event(label_type='transaction') already
populated in Phase C.

Fresh-off-IL hitters historically underperform their season aggregate
in the first 5 games back as they shake off rust.  Pitchers similarly
have reduced velocity / inflated WHIP in their first start back.

Usage:
    python scripts/backfill_il_return_flag.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date as DateType, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-il-return-stub")

from app.core import historical_db  # noqa: E402

# Detection mode: parse the transaction `description` text for activation
# keywords rather than relying on type_code alone.  MLB Stats API uses the
# same `SC` (Status Change) typeCode for BOTH "placed on IL" and "activated
# from IL", so type_code is insufficient.  The description text is the
# authoritative signal.
#
# We also include the SE (Selected) typeCode unconditionally — those are
# call-ups from the minors, which behave like IL-returns for prediction
# purposes (first ~7 days are below-rate while the player adjusts).
ACTIVATION_KEYWORDS = (
    "activated",      # "activated 1B Curtis Mead" / "activated from the 10-day IL"
    "recalled",       # "recalled RHP Bryan Woo"
    "returned to active",
    "selected the contract",  # call-up from minors (SE typeCode)
)
WINDOW_DAYS = 7

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_il_return_flag")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # Build {mlb_id: list[(effective_date, description)]} from label_event.
        # Description is the authoritative signal — typeCode SC fires for
        # both "placed on IL" and "activated from IL".
        cur = conn.execute(
            "SELECT mlb_id, label_text FROM label_event "
            "WHERE label_type = 'transaction'"
        )
        per_player: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for r in cur.fetchall():
            try:
                payload = json.loads(r["label_text"] or "{}")
            except json.JSONDecodeError:
                continue
            desc = (payload.get("description") or "").lower()
            eff_date = payload.get("effective_date")
            if not eff_date:
                continue
            per_player[r["mlb_id"]].append((eff_date, desc))
        log.info("transaction-indexed players: %d", len(per_player))

        if args.force:
            where = "WHERE 1=1"
        else:
            where = "WHERE is_fresh_off_il IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, mlb_id FROM player_slate {where}"
        )
        targets = cur.fetchall()
        log.info("player rows to populate: %d", len(targets))

        updates = 0
        flag_count = 0
        for t in targets:
            slate_d = DateType.fromisoformat(t["slate_date"])
            cutoff = (slate_d - timedelta(days=WINDOW_DAYS)).isoformat()
            history = per_player.get(t["mlb_id"], [])
            is_fresh = any(
                eff_date >= cutoff and eff_date < t["slate_date"]
                and any(kw in desc for kw in ACTIVATION_KEYWORDS)
                for eff_date, desc in history
            )
            if is_fresh:
                flag_count += 1
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"],
                {"is_fresh_off_il": int(is_fresh)},
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (is_fresh_off_il=1: %d)", updates, flag_count)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0 and not os.environ.get("HISTORICAL_DB"):
        from scripts.export_historical_csvs import export_all
        export_all()
    sys.exit(rc)
