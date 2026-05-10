"""Backfill Vegas line opening snapshots onto slate_game.

Tier 1 D4 of the May 2026 cleanup-and-add sweep.

Source: SportsbookReview (https://www.sportsbookreview.com/betting-odds/mlb-baseball/)
— scrape the embedded `__NEXT_DATA__` JSON which carries openingLine /
currentLine for every sportsbook per game.  Free, no API key, fully
historical (the page accepts ?date=YYYY-MM-DD).  Replaces the earlier
The-Odds-API path which required a paid historical-tier subscription.

Per-game we capture:
  opening_total           — consensus O/U at the first available bookmaker snapshot
  opening_home_moneyline  — consensus home ML at the same snapshot
  opening_away_moneyline  — consensus away ML at the same snapshot
  line_open_at            — ISO timestamp of the snapshot (game's startDate is
                            the closest stable proxy SBR exposes; openingLine
                            doesn't carry its own timestamp on the public page)

Calibration unlock: ML drift (closing_ml − opening_ml) is a sharp-money
signal distinct from the closing line itself.  Lets V14's
predict_popularity_bucket add a "smart money is on this favorite" feature.

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
import re
import statistics
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-vegas-line-movement-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".line_movement_cache"
SBR_BASE_ML = "https://www.sportsbookreview.com/betting-odds/mlb-baseball/"
SBR_BASE_TOTALS = "https://www.sportsbookreview.com/betting-odds/mlb-baseball/totals/"
HTTP_TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0"}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_vegas_line_movement")

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>'
)


def _fetch_next_data(url: str) -> dict | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    except Exception as e:
        log.warning("fetch failed for %s: %s", url, e)
        return None
    if r.status_code != 200:
        log.warning("fetch returned %s for %s", r.status_code, url)
        return None
    m = NEXT_DATA_RE.search(r.text)
    if not m:
        log.warning("no __NEXT_DATA__ in %s", url)
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        log.warning("__NEXT_DATA__ JSON parse failed for %s: %s", url, e)
        return None


def _consensus_int(values: list[int | None]) -> int | None:
    """Median of non-null integer values, rounded.  Used for ML consensus
    across sportsbooks (different books open the same fav at -130 / -135 / -140,
    median is the cleanest single number)."""
    vs = [int(v) for v in values if v is not None]
    if not vs:
        return None
    return int(round(statistics.median(vs)))


def _consensus_float(values: list[float | None]) -> float | None:
    vs = [float(v) for v in values if v is not None]
    if not vs:
        return None
    return float(statistics.median(vs))


def _fetch_opening_snapshot(slate_date: str) -> dict:
    """Return {(home_abbr, away_abbr): {opening_total, opening_home_moneyline,
    opening_away_moneyline, line_open_at}} for the slate."""
    cache_file = CACHE_DIR / f"{slate_date}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass

    out: dict = {}

    # ---- Moneyline page ----
    ml_data = _fetch_next_data(f"{SBR_BASE_ML}?date={slate_date}")
    if ml_data:
        ml_games = (
            ml_data.get("props", {})
            .get("pageProps", {})
            .get("oddsTables", [{}])[0]
            .get("oddsTableModel", {})
            .get("gameRows", [])
        )
        for g in ml_games:
            gv = g.get("gameView", {})
            home = (gv.get("homeTeam") or {}).get("shortName")
            away = (gv.get("awayTeam") or {}).get("shortName")
            if not home or not away:
                continue
            ml_homes = []
            ml_aways = []
            for ov in g.get("oddsViews", []) or []:
                if not ov:
                    continue
                ol = ov.get("openingLine") or {}
                if ol.get("homeOdds") is not None:
                    ml_homes.append(ol["homeOdds"])
                if ol.get("awayOdds") is not None:
                    ml_aways.append(ol["awayOdds"])
            key = f"{home}|{away}"
            out.setdefault(key, {})
            out[key]["opening_home_moneyline"] = _consensus_int(ml_homes)
            out[key]["opening_away_moneyline"] = _consensus_int(ml_aways)
            out[key]["line_open_at"] = gv.get("startDate")

    # ---- Totals page ----
    tot_data = _fetch_next_data(f"{SBR_BASE_TOTALS}?date={slate_date}")
    if tot_data:
        tot_games = (
            tot_data.get("props", {})
            .get("pageProps", {})
            .get("oddsTables", [{}])[0]
            .get("oddsTableModel", {})
            .get("gameRows", [])
        )
        for g in tot_games:
            gv = g.get("gameView", {})
            home = (gv.get("homeTeam") or {}).get("shortName")
            away = (gv.get("awayTeam") or {}).get("shortName")
            if not home or not away:
                continue
            totals = []
            for ov in g.get("oddsViews", []) or []:
                if not ov:
                    continue
                ol = ov.get("openingLine") or {}
                if ol.get("total") is not None:
                    totals.append(ol["total"])
            key = f"{home}|{away}"
            out.setdefault(key, {})
            out[key]["opening_total"] = _consensus_float(totals)
            out[key].setdefault("line_open_at", gv.get("startDate"))

    if out:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(out))
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
            where = "WHERE opening_total IS NULL AND opening_home_moneyline IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, game_pk, home_team, away_team FROM slate_game "
            f"{where} ORDER BY slate_date, game_pk"
        )
        targets = cur.fetchall()
        unique_dates = sorted({t["slate_date"] for t in targets})
        log.info("targets: %d games across %d dates", len(targets), len(unique_dates))

        date_snapshots = {d: _fetch_opening_snapshot(d) for d in unique_dates}

        updates = 0
        misses = 0
        for t in targets:
            snap = date_snapshots.get(t["slate_date"]) or {}
            key = f"{t['home_team']}|{t['away_team']}"
            rec = snap.get(key)
            if not rec or not any(
                rec.get(k) is not None
                for k in ("opening_total", "opening_home_moneyline", "opening_away_moneyline")
            ):
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
