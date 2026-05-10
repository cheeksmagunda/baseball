"""Backfill per-batter, per-pitch-type wOBA splits into batter_pitch_type_woba.

Tier 3 D13 of the May 2026 cleanup-and-add sweep — flagged "deferred" by
CLAUDE.md V10.8.  Big lift; ships behind the schema so the integration
question is no longer "do we have a place for this data" but "is the
calibration improvement worth the runtime plumbing".

Source: Baseball Savant batter splits leaderboard filtered by pitch type.
For each (mlb_id, pitch_type) pair we capture pa_count + season-aggregate
wOBA.

Pipeline plumb (separate change, not in this script): replace V10.8's
"simplified xwOBA-against single number" approach in score_batter_matchup
with the full crosstab — for each opposing pitcher, weight the batter's
per-pitch-type wOBA by the pitcher's arsenal usage % to get the true
expected matchup wOBA.

Storage cost: ~500 batters × 11 pitch types × 43 slates ≈ 235k rows.

Cache: scripts/output/.batter_pitch_splits_cache/<season>.json — one
file per season holding the full leaderboard.

Usage:
    python scripts/backfill_batter_pitch_type_splits.py
    python scripts/backfill_batter_pitch_type_splits.py --season 2026
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-batter-pitch-splits-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".batter_pitch_splits_cache"
HTTP_TIMEOUT = 60

# 11 pitch types we already track in the pitcher arsenal columns
PITCH_TYPES = ("FF", "SI", "FC", "SL", "ST", "CU", "KC", "CH", "FS", "KN", "SV")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_batter_pitch_type_splits")


def _safe_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fetch_one_pitch_type(season: int, pitch_type: str) -> list[dict]:
    """Per-batter rows for one (season, pitch_type)."""
    cache_file = CACHE_DIR / f"{season}_{pitch_type}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass
    url = (
        "https://baseballsavant.mlb.com/leaderboard/pitch-type-splits"
        f"?type=batter&year={season}&min_pa=20&pitch_type={pitch_type}&csv=true"
    )
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            log.warning("pitch_type=%s fetch returned %s", pitch_type, r.status_code)
            return []
        reader = csv.DictReader(io.StringIO(r.text))
        out: list[dict] = []
        for row in reader:
            try:
                pid = int(row.get("player_id") or 0)
            except ValueError:
                continue
            if pid <= 0:
                continue
            out.append({
                "mlb_id": pid,
                "pa_count": _safe_int(row.get("pa")),
                "woba": _safe_float(row.get("woba")),
            })
    except Exception as e:
        log.warning("pitch_type=%s fetch failed: %s", pitch_type, e)
        return []
    if out:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(out))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    # Pull all 11 pitch types
    by_pitch: dict[str, list[dict]] = {}
    for pt in PITCH_TYPES:
        by_pitch[pt] = _fetch_one_pitch_type(args.season, pt)
    total_rows = sum(len(v) for v in by_pitch.values())
    log.info("loaded %d pitch-type rows across %d types",
             total_rows, sum(1 for v in by_pitch.values() if v))

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # Skip-detect by checking if any rows already exist for the season.
        if not args.force:
            cur = conn.execute(
                "SELECT 1 FROM batter_pitch_type_woba WHERE slate_date LIKE ? LIMIT 1",
                (f"{args.season}-%",),
            )
            if cur.fetchone() is not None:
                log.info("season %s already has rows — pass --force to refresh",
                         args.season)
                return 0

        # Map (mlb_id, pitch_type) → row.  Then for each player_slate row,
        # insert one row per pitch type the batter has data for.
        cur = conn.execute(
            "SELECT slate_date, mlb_id FROM player_slate "
            "WHERE position NOT IN ('P','SP','RP','TWP') "
            "ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        log.info("batter rows to expand: %d", len(targets))

        observed_at = datetime.now(timezone.utc).isoformat()
        inserts = 0
        for t in targets:
            for pt, rows in by_pitch.items():
                hit = next((r for r in rows if r["mlb_id"] == t["mlb_id"]), None)
                if not hit or hit.get("woba") is None:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO batter_pitch_type_woba "
                    "(slate_date, mlb_id, pitch_type, pa_count, woba, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (t["slate_date"], t["mlb_id"], pt,
                     hit.get("pa_count"), hit.get("woba"), observed_at),
                )
                inserts += 1
        conn.commit()
        log.info("INSERT batter_pitch_type_woba rows: %d", inserts)
        if inserts == 0 and total_rows == 0 and targets:
            log.warning(
                "0 rows written — Savant pitch-type-splits leaderboard may "
                "have a different URL than assumed.  Schema is in place; "
                "wire the actual endpoint when calibration motivates."
            )
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
