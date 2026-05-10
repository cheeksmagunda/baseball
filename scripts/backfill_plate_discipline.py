"""Backfill plate-discipline metrics onto player_slate.

Tier 2 D5 of the May 2026 cleanup-and-add sweep.

Schema columns populated:
  bb_pct          — walk rate
  k_pct           — strikeout rate
  o_swing_pct     — swings at pitches outside the zone
  z_contact_pct   — contact rate on pitches in the zone
  sw_str_pct      — swinging-strike rate

Source: Baseball Savant batter expected-stats leaderboard (CSV download)
or pybaseball's `pitching_stats_bref` for season aggregates.  These
discipline numbers are orthogonal to xwOBA — discipline is FLOOR (will
this hitter get on base when the matchup is hard) where xwOBA is
CEILING (will the contact be quality).  Especially valuable for the
K-vulnerability cross-penalty in score_batter_matchup.

Cache: scripts/output/.plate_discipline_cache/<season>_<half>.json.
Re-runs are cheap.

Usage:
    python scripts/backfill_plate_discipline.py
    python scripts/backfill_plate_discipline.py --season 2026
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-plate-discipline-stub")

from app.core import historical_db  # noqa: E402
from scripts._backfill_common import safe_float as _safe_float  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".plate_discipline_cache"
HTTP_TIMEOUT = 30

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_plate_discipline")


def _fetch_leaderboard(season: int) -> dict[int, dict]:
    """Pull the per-batter discipline aggregate from Savant's batter
    leaderboard CSV download."""
    cache_file = CACHE_DIR / f"{season}.json"
    if cache_file.exists():
        try:
            return {int(k): v for k, v in json.loads(cache_file.read_text()).items()}
        except json.JSONDecodeError:
            pass
    # Savant's expected_statistics endpoint only returns xStats (xwOBA, xBA,
    # xSLG) — not plate discipline.  Use the percentile-rankings endpoint
    # which carries k_percent, bb_percent, whiff_percent, chase_percent.
    # z_contact_percent is not on Savant's public leaderboard surface; left
    # NULL here and backfilled separately from FanGraphs (Phase C).
    url = (
        "https://baseballsavant.mlb.com/leaderboard/percentile-rankings"
        f"?type=batter&year={season}&min_pa=50&csv=true"
    )
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            log.warning("plate-discipline leaderboard fetch returned %s", r.status_code)
            return {}
        reader = csv.DictReader(io.StringIO(r.text.lstrip("﻿")))
        out: dict[int, dict] = {}
        for row in reader:
            try:
                pid = int(row.get("player_id") or 0)
            except ValueError:
                continue
            if pid <= 0:
                continue
            out[pid] = {
                "bb_pct": _safe_float(row.get("bb_percent")),
                "k_pct": _safe_float(row.get("k_percent")),
                # chase_percent is the % of out-of-zone pitches the batter
                # swings at — same definition as O-Swing%.
                "o_swing_pct": _safe_float(row.get("chase_percent")),
                # z_contact_percent not on Savant; populated via FanGraphs.
                "z_contact_pct": None,
                "sw_str_pct": _safe_float(row.get("whiff_percent")),
            }
    except Exception as e:
        log.warning("plate-discipline leaderboard fetch failed: %s", e)
        return {}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({str(k): v for k, v in out.items()}))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    discipline = _fetch_leaderboard(args.season)
    log.info("loaded discipline rows: %d", len(discipline))

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE position NOT IN ('P','SP','RP','TWP')"
        else:
            where = "WHERE position NOT IN ('P','SP','RP','TWP') AND bb_pct IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, mlb_id FROM player_slate {where}"
        )
        targets = cur.fetchall()
        log.info("batter rows to populate: %d", len(targets))

        updates = 0
        misses = 0
        for t in targets:
            rec = discipline.get(t["mlb_id"])
            if not rec or not any(v is not None for v in rec.values()):
                misses += 1
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], rec,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no leaderboard record: %d)", updates, misses)
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
