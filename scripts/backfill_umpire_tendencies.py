"""Backfill HP umpire historical K%/BB% tendencies into umpire_dim.

Tier 1 D1 of the May 2026 cleanup-and-add sweep.

Source: Umpire Scorecards public CSV exports
(https://umpscorecards.com/single_umpire/?u=<id>) — fetches the season-
total page per umpire and parses the called-strike%, K-rate-vs-league,
BB-rate-vs-league, and runs-above-avg numbers.

Operates against the set of HP umpire IDs already populated on
slate_game.ump_hp_id.  One API call per (ump_id, season) pair, cached to
scripts/output/.umpire_cache/<season>_<ump_id>.json so re-runs are cheap.

In a sandboxed environment with no internet, the script logs the IDs
that need fetching and exits 0 — the schema is in place, run it once
the network is reachable.

2026 ABS caveat: ~2% of pitches challenged.  98% still ride on the
human zone, so the signal is real but compressed.  Magnitude cap at
scoring time should mirror SCORING_FRAMING_K_RATE_MAX_ADJ.

Usage:
    python scripts/backfill_umpire_tendencies.py
    python scripts/backfill_umpire_tendencies.py --season 2026
"""
from __future__ import annotations

import argparse
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-umpire-tendencies-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".umpire_cache"
HTTP_TIMEOUT = 15

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_umpire_tendencies")


def _cached_or_fetch(ump_id: int, season: int) -> dict | None:
    """Return the umpire's season aggregate, fetching from Umpire Scorecards
    if not cached.  Returns None on network failure."""
    cf = CACHE_DIR / f"{season}_{ump_id}.json"
    if cf.exists():
        try:
            return json.loads(cf.read_text())
        except json.JSONDecodeError:
            pass
    url = f"https://umpscorecards.com/api/umpire/{ump_id}?season={season}"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            log.warning("ump_id=%s season=%s fetch returned %s", ump_id, season, r.status_code)
            return None
        data = r.json()
    except Exception as e:
        log.warning("ump_id=%s season=%s fetch failed: %s", ump_id, season, e)
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf.write_text(json.dumps(data))
    return data


def _extract(data: dict) -> dict:
    """Map the Umpire Scorecards JSON shape to umpire_dim columns."""
    if not data:
        return {}
    return {
        "ump_name": data.get("name"),
        "games_called": data.get("games"),
        "called_strike_pct": data.get("calledStrikePct"),
        "k_rate_vs_league": data.get("kRateVsLeague"),
        "bb_rate_vs_league": data.get("bbRateVsLeague"),
        "x_runs_above_avg": data.get("xRunsAboveAvg"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        cur = conn.execute(
            "SELECT DISTINCT ump_hp_id FROM slate_game "
            "WHERE ump_hp_id IS NOT NULL ORDER BY ump_hp_id"
        )
        ump_ids = [r["ump_hp_id"] for r in cur.fetchall()]
        log.info("unique HP umpire ids: %d", len(ump_ids))

        observed_at = datetime.now(timezone.utc).isoformat()
        upserts = 0
        misses = 0
        for ump_id in ump_ids:
            data = _cached_or_fetch(ump_id, args.season)
            ext = _extract(data) if data else {}
            if not any(v is not None for v in ext.values()):
                misses += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO umpire_dim "
                "(ump_id, season, ump_name, games_called, called_strike_pct, "
                " k_rate_vs_league, bb_rate_vs_league, x_runs_above_avg, "
                " observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ump_id, args.season, ext.get("ump_name"),
                    ext.get("games_called"), ext.get("called_strike_pct"),
                    ext.get("k_rate_vs_league"), ext.get("bb_rate_vs_league"),
                    ext.get("x_runs_above_avg"), observed_at,
                ),
            )
            upserts += 1
        conn.commit()
        log.info("UPSERT rows: %d (cache miss / no record: %d)", upserts, misses)
        if upserts == 0 and ump_ids:
            log.warning(
                "0 rows written — likely network unreachable.  Schema "
                "is in place; re-run when reachable to populate."
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
