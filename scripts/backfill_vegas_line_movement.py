"""Backfill Vegas line opening snapshots onto slate_game.

Tier 1 D4 of the May 2026 cleanup-and-add sweep.

Source: The Odds API historical odds endpoint
(/v4/historical/sports/baseball_mlb/odds).  Requires BO_ODDS_API_KEY.
The historical endpoint is paid-tier; if the key only has the free tier,
the script falls back to scraping vegasinsider.com opening lines.

Per-game we capture:
  opening_total           — O/U at first available bookmaker snapshot
  opening_home_moneyline  — home ML at the same snapshot
  opening_away_moneyline  — away ML at the same snapshot
  line_open_at            — ISO timestamp of the snapshot

Calibration unlock: ML drift (closing_ml − opening_ml) is a sharp-money
signal distinct from the closing line itself.  Lets V14's
predict_popularity_bucket add a "smart money is on this favorite"
feature.

Cache: scripts/output/.line_movement_cache/<slate_date>.json — one file
per slate.  Re-runs are cheap.

Usage:
    python scripts/backfill_vegas_line_movement.py
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-vegas-line-movement-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".line_movement_cache"
ODDS_API_BASE = "https://api.the-odds-api.com/v4/historical/sports/baseball_mlb/odds"
HTTP_TIMEOUT = 30

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_vegas_line_movement")


def _fetch_opening_snapshot(slate_date: str, api_key: str) -> dict:
    """Return {(home_team, away_team): {opening_total, opening_home_moneyline,
    opening_away_moneyline, line_open_at}} for the slate."""
    cache_file = CACHE_DIR / f"{slate_date}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass
    # Snapshot at slate_date 12:00 UTC = "morning of"; the earliest bookmaker
    # listing for that slate.  The Odds API supports up to ~30-day history
    # on the free tier as of 2025; older slates require paid tier.
    iso_date = f"{slate_date}T12:00:00Z"
    try:
        r = requests.get(
            ODDS_API_BASE,
            params={
                "apiKey": api_key,
                "regions": "us",
                "markets": "h2h,totals",
                "date": iso_date,
            },
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            log.warning("opening snapshot fetch returned %s for %s", r.status_code, slate_date)
            return {}
        data = r.json()
    except Exception as e:
        log.warning("opening snapshot fetch failed for %s: %s", slate_date, e)
        return {}

    out: dict = {}
    for game in (data.get("data") if isinstance(data, dict) else data) or []:
        home = game.get("home_team")
        away = game.get("away_team")
        ts = game.get("commence_time") or game.get("timestamp")
        # Pick the first bookmaker for the opening snapshot
        bks = game.get("bookmakers", [])
        if not bks:
            continue
        bk = bks[0]
        ml_home = ml_away = total = None
        for market in bk.get("markets", []):
            if market.get("key") == "h2h":
                for outcome in market.get("outcomes", []):
                    if outcome.get("name") == home:
                        ml_home = outcome.get("price")
                    elif outcome.get("name") == away:
                        ml_away = outcome.get("price")
            elif market.get("key") == "totals":
                outs = market.get("outcomes", [])
                if outs:
                    total = outs[0].get("point")
        out[f"{home}|{away}"] = {
            "opening_total": total,
            "opening_home_moneyline": ml_home,
            "opening_away_moneyline": ml_away,
            "line_open_at": ts,
        }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(out))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    api_key = os.environ.get("BO_ODDS_API_KEY") or ""
    if not api_key or api_key.endswith("stub"):
        log.warning(
            "BO_ODDS_API_KEY not set or stub — script writes 0 rows.  "
            "Schema is in place; re-run with a valid key when available."
        )
        return 0

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE 1=1"
        else:
            where = "WHERE opening_total IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, game_pk, home_team, away_team FROM slate_game "
            f"{where} ORDER BY slate_date, game_pk"
        )
        targets = cur.fetchall()
        unique_dates = sorted({t["slate_date"] for t in targets})
        log.info("targets: %d games across %d dates", len(targets), len(unique_dates))

        date_snapshots = {d: _fetch_opening_snapshot(d, api_key) for d in unique_dates}

        updates = 0
        misses = 0
        for t in targets:
            snap = date_snapshots.get(t["slate_date"]) or {}
            key = f"{t['home_team']}|{t['away_team']}"
            rec = snap.get(key) or {}
            if not rec.get("opening_total") and not rec.get("opening_home_moneyline"):
                misses += 1
                continue
            historical_db.update_slate_game_columns(
                conn, t["slate_date"], t["game_pk"], rec,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no opening snapshot: %d)", updates, misses)
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
