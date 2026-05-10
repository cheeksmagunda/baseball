"""Backfill park × handedness HR factors onto slate_game.

Phase C add (May 2026).  Coors plays differently for LHB vs RHB —
aggregated park_hr_factor masks the asymmetry.  This adds the per-side
index from Savant's statcast-park-factors leaderboard.

Source: Savant `/leaderboard/statcast-park-factors` HTML page, embedded
JSON `var data = [...]`.  Default 3-year rolling window centred on the
slate season — that's what the leaderboard exposes.

The index is normalised so 100 = league average; >100 = HR-friendly,
<100 = HR-suppressing.  We store as a float (e.g. 1.13 = 13% boost),
matching the convention already used for `park_hr_factor`.

Usage:
    python scripts/backfill_park_hand_factors.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-park-hand-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".park_hand_cache"
HEADERS = {"User-Agent": "Mozilla/5.0"}
DATA_RE = re.compile(r"var\s+data\s*=\s*(\[[\s\S]*?\]);")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_park_hand_factors")


def _fetch(season: int, bat_side: str) -> list[dict]:
    cache_file = CACHE_DIR / f"{season}_{bat_side}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass
    url = (
        "https://baseballsavant.mlb.com/leaderboard/statcast-park-factors"
        f"?type=year&year={season}&batSide={bat_side}&stat=index_wOBA&condition=All&rolling="
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
    except Exception as e:
        log.warning("park factors fetch failed: %s", e)
        return []
    if r.status_code != 200:
        log.warning("park factors fetch returned %s", r.status_code)
        return []
    m = DATA_RE.search(r.text)
    if not m:
        log.warning("no embedded var data in park factors HTML")
        return []
    try:
        rows = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(rows))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    # Fetch L and R separately
    lhb_rows = _fetch(args.season, "L")
    rhb_rows = _fetch(args.season, "R")
    log.info("fetched L=%d R=%d park rows", len(lhb_rows), len(rhb_rows))

    # Index by venue_id → index_hr (the HR-specific factor)
    lhb_lookup = {int(r["venue_id"]): float(r.get("index_hr", 100)) / 100.0 for r in lhb_rows}
    rhb_lookup = {int(r["venue_id"]): float(r.get("index_hr", 100)) / 100.0 for r in rhb_rows}

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE venue_id IS NOT NULL"
        else:
            where = "WHERE venue_id IS NOT NULL AND park_hr_factor_lhb IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, game_pk, venue_id FROM slate_game {where} "
            "ORDER BY slate_date, game_pk"
        )
        targets = cur.fetchall()
        log.info("targets: %d games", len(targets))

        updates = 0
        misses = 0
        for t in targets:
            vid = t["venue_id"]
            lhb = lhb_lookup.get(vid)
            rhb = rhb_lookup.get(vid)
            if lhb is None and rhb is None:
                misses += 1
                continue
            historical_db.update_slate_game_columns(
                conn, t["slate_date"], t["game_pk"],
                {"park_hr_factor_lhb": lhb, "park_hr_factor_rhb": rhb},
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no factor: %d)", updates, misses)
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
