"""Backfill per-batter bat-tracking metrics onto player_slate from Savant's
public bat-tracking leaderboard (2024+).

External-only — every column is a verbatim value from the leaderboard CSV.

Schema columns populated:
  avg_bat_speed_mph        — average bat speed across competitive swings
  hard_swing_rate          — % of swings >= 75 mph
  swing_length_ft          — average bat path length
  squared_up_per_swing     — squared-up contact rate per swing
  blast_per_swing          — top-tier "blast" contact rate per swing
  swords_count             — competitive swings + miss measure

Usage:
    python scripts/backfill_bat_tracking.py
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-bat-tracking-stub")

from app.core import historical_db  # noqa: E402
from scripts._backfill_common import safe_int as _safe_int, safe_float as _safe_float  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_bat_tracking")


def fetch_bat_tracking(min_swings: int = 50):
    url = (
        "https://baseballsavant.mlb.com/leaderboard/bat-tracking"
        f"?attackZone=&batSide=&count=&team=&min={min_swings}&sort=4,1&csv=true"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    text = r.text
    if text.startswith("﻿"):
        text = text[1:]
    rows = list(csv.DictReader(io.StringIO(text)))
    log.info("Savant bat-tracking: %d batters (min %d swings)", len(rows), min_swings)
    out: dict[int, dict] = {}
    for r2 in rows:
        try:
            mid = int(r2.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if mid <= 0:
            continue
        out[mid] = {
            "avg_bat_speed_mph": _safe_float(r2.get("avg_bat_speed")),
            "hard_swing_rate": _safe_float(r2.get("hard_swing_rate")),
            "swing_length_ft": _safe_float(r2.get("swing_length")),
            "squared_up_per_swing": _safe_float(r2.get("squared_up_per_swing")),
            "blast_per_swing": _safe_float(r2.get("blast_per_swing")),
            "swords_count": _safe_int(r2.get("swords")),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-swings", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    bat = fetch_bat_tracking(args.min_swings)
    if args.dry_run:
        log.info("sample: %s", next(iter(bat.items()), None))
        return 0

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE mlb_id > 0"
        else:
            where = "WHERE mlb_id > 0 AND avg_bat_speed_mph IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, mlb_id FROM player_slate {where} "
            f"ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        log.info("rows to populate: %d", len(targets))
        updates = 0
        for t in targets:
            rec = bat.get(t["mlb_id"])
            if not rec:
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], rec,
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
        import sys as _sys
        from pathlib import Path as _Path
        _repo = _Path(__file__).resolve().parents[1]
        if str(_repo) not in _sys.path:
            _sys.path.insert(0, str(_repo))
        # Skip the on-disk /data/ export when we're operating against a
        # non-canonical DB (audit reproducibility chain) so the canonical
        # CSV/JSON files in /data/ are not clobbered.
        import os as _os
        if not _os.environ.get('HISTORICAL_DB'):
            from scripts.export_historical_csvs import export_all
            export_all()
    sys.exit(rc)
