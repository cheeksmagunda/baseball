"""Backfill DFS-site projected ownership onto player_slate.

Tier 2 D9 of the May 2026 cleanup-and-add sweep.

Schema columns populated:
  dfs_projected_ownership_pct  — consensus pre-lock projection
  dfs_projection_source        — vendor providing the projection

Source: scrape FantasyPros (`fantasypros.com/mlb/projections/`) or
RotoGrinders (`rotogrinders.com/projections/mlb`).  Both publish projected
ownership 6-12 hrs before lock.

Calibration unlock: V14's `leverage_factor` is currently built from a
rule-based predictor (team market tier, fame flags, batting order, MP
rolling index).  A real DFS-site ownership projection is a calibration
TARGET / sanity check — does our predicted bucket agree with the field
consensus?  When they diverge, which is closer to actual outcomes?

Cache: scripts/output/.dfs_ownership_cache/<slate_date>.json — one file
per slate.

Usage:
    python scripts/backfill_dfs_ownership.py
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-dfs-ownership-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".dfs_ownership_cache"
HTTP_TIMEOUT = 20

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_dfs_ownership")


def _fetch_fantasypros(slate_date: str) -> dict[tuple[str, str], float]:
    """Returns {(player_name, team): projected_ownership_pct}.

    FantasyPros published-ownership endpoint.  Best-effort — page layout
    changes are common; if scrape fails we skip the slate.
    """
    cache_file = CACHE_DIR / f"{slate_date}.json"
    if cache_file.exists():
        try:
            return {tuple(json.loads(k)): v for k, v in json.loads(cache_file.read_text()).items()}
        except (json.JSONDecodeError, ValueError):
            pass
    url = f"https://www.fantasypros.com/mlb/projections/ownership.php?date={slate_date}"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            log.warning("fantasypros fetch returned %s for %s", r.status_code, slate_date)
            return {}
    except Exception as e:
        log.warning("fantasypros fetch failed for %s: %s", slate_date, e)
        return {}
    # Very permissive scrape — pulls (name, team, pct) tuples from any
    # row matching the table-row shape.  Real impl would use BeautifulSoup
    # but this stays dependency-light.
    out: dict[tuple[str, str], float] = {}
    pattern = re.compile(
        r'<tr[^>]*>.*?<a[^>]*>([^<]+)</a>.*?>([A-Z]{2,4})<.*?(\d+(?:\.\d+)?)%',
        re.DOTALL,
    )
    for m in pattern.finditer(r.text):
        try:
            out[(m.group(1).strip(), m.group(2).strip())] = float(m.group(3))
        except ValueError:
            continue
    if out:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({json.dumps(list(k)): v for k, v in out.items()}))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE 1=1"
        else:
            where = "WHERE dfs_projected_ownership_pct IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, mlb_id, player_name, team FROM player_slate "
            f"{where} ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        unique_dates = sorted({t["slate_date"] for t in targets})
        log.info("targets: %d players across %d dates", len(targets), len(unique_dates))

        date_lookups = {d: _fetch_fantasypros(d) for d in unique_dates}

        updates = 0
        misses = 0
        for t in targets:
            lookup = date_lookups.get(t["slate_date"]) or {}
            pct = lookup.get((t["player_name"], t["team"]))
            if pct is None:
                misses += 1
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"],
                {"dfs_projected_ownership_pct": pct,
                 "dfs_projection_source": "fantasypros"},
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no projection: %d)", updates, misses)
        if updates == 0 and len(targets) > 0:
            log.warning(
                "0 rows written — likely network unreachable or layout changed.  "
                "Schema is in place; re-run when reachable."
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
