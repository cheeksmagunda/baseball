"""Backfill per-batter sprint speed + defensive metrics onto player_slate.

External-only (Baseball Savant leaderboards):
  sprint_speed_fps         — pybaseball.statcast_sprint_speed
  hp_to_first_sec          — same source (home-to-first time)
  competitive_runs         — same source (count of qualifying runs)
  outs_above_avg           — Savant outs_above_average leaderboard CSV
  fielding_runs_prevented  — same Savant CSV

The OAA leaderboard CSV from Savant has a BOM-prefixed first column
that confuses naive csv.DictReader; we strip it explicitly before
parsing.

Usage:
    python scripts/backfill_sprint_oaa.py
    python scripts/backfill_sprint_oaa.py --force
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-sprint-oaa-stub")

from app.core import historical_db  # noqa: E402
from scripts._backfill_common import safe_int as _safe_int, safe_float as _safe_float  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_sprint_oaa")


def fetch_sprint_speed(season: int):
    """Returns {mlb_id: {sprint_speed_fps, hp_to_first_sec, competitive_runs}}."""
    from pybaseball import statcast_sprint_speed

    df = statcast_sprint_speed(season, min_opp=10)
    log.info("Savant sprint speed: %d players", len(df))
    out: dict[int, dict] = {}
    for _, r in df.iterrows():
        try:
            mid = int(r["player_id"])
        except (TypeError, ValueError, KeyError):
            continue
        out[mid] = {
            "sprint_speed_fps": _safe_float(r.get("sprint_speed")),
            "hp_to_first_sec": _safe_float(r.get("hp_to_1b")),
            "competitive_runs": _safe_int(r.get("competitive_runs")),
        }
    return out


def fetch_oaa(season: int):
    """Returns {mlb_id: {outs_above_avg, fielding_runs_prevented}}."""
    url = (
        "https://baseballsavant.mlb.com/leaderboard/outs_above_average"
        f"?type=Fielder&year={season}&team=&min=q&csv=true"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    text = r.text
    # Savant CSVs prepend a UTF-8 BOM; strip it cleanly.
    if text.startswith("﻿"):
        text = text[1:]
    rows = list(csv.DictReader(io.StringIO(text)))
    log.info("Savant OAA: %d rows", len(rows))
    out: dict[int, dict] = {}
    for r2 in rows:
        try:
            mid = int(r2.get("player_id") or 0)
        except (TypeError, ValueError):
            continue
        if mid <= 0:
            continue
        out[mid] = {
            "outs_above_avg": _safe_int(r2.get("outs_above_average")),
            "fielding_runs_prevented": _safe_int(r2.get("fielding_runs_prevented")),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    sprint = fetch_sprint_speed(args.season)
    oaa = fetch_oaa(args.season)

    # Merge keys
    all_ids = set(sprint) | set(oaa)
    log.info("union of mlb_ids in either leaderboard: %d", len(all_ids))

    if args.dry_run:
        sample = next(iter(all_ids), None)
        if sample:
            merged = {**sprint.get(sample, {}), **oaa.get(sample, {})}
            log.info("sample mlb_id=%d: %s", sample, merged)
        return 0

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE mlb_id > 0"
        else:
            where = "WHERE mlb_id > 0 AND sprint_speed_fps IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, mlb_id FROM player_slate {where} "
            f"ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        log.info("rows to populate: %d", len(targets))

        updates = 0
        for t in targets:
            mid = t["mlb_id"]
            merged = {}
            merged.update(sprint.get(mid, {}))
            merged.update(oaa.get(mid, {}))
            if not any(v is not None for v in merged.values()):
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], mid, merged,
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
