"""Backfill external game-level data onto slate_game from the MLB Stats API.

Pulls the live-feed gumbo endpoint (`/api/v1.1/game/{game_pk}/feed/live`) for
every (slate_date, game_pk) pair in the corpus and writes the following
EXTERNAL fields directly to data/historical.db:

  - Game info: attendance, day_night
  - Venue static: id, name, capacity, surface (Grass/Turf), roof_type,
    elevation_ft, latitude, longitude, timezone, field-dimension feet
    (lf_line through rf_line)
  - Umpire crew: HP umpire id and name
  - Catcher: actual home / away catcher mlb_id (from boxscore battingOrder
    cross-referenced against position abbreviations)

May 2026 cleanup sweep dropped:
  - game_duration_minutes (post-game-only, not a predictor)
  - weather_condition (free-text MLB field; Open-Meteo is more reliable)
  - ump_1b_id / ump_2b_id / ump_3b_id (only HP ump signals K-rate)

These are pure post-game external observables — no derived / pipeline-
computed signals.

Usage:
    python scripts/backfill_game_externals.py
    python scripts/backfill_game_externals.py --dry-run
    python scripts/backfill_game_externals.py --since 2026-04-01

Cache: stores per-game JSON at scripts/output/.game_externals_cache/<game_pk>.json
so re-runs are cheap.  Re-run after a new slate is scraped to fill in the
new game_pks.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-game-externals-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".game_externals_cache"
MLB_API = "https://statsapi.mlb.com/api/v1.1"
HTTP_TIMEOUT = 20
MAX_WORKERS = 12

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_game_externals")


def fetch_game(game_pk: int) -> dict | None:
    """Fetch the gumbo feed for a single game_pk, with disk cache."""
    cache_file = CACHE_DIR / f"{game_pk}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass
    try:
        r = requests.get(f"{MLB_API}/game/{game_pk}/feed/live", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log.warning("game_pk=%s fetch failed: %s", game_pk, e)
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(r.text)
    return r.json()


def _safe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _safe_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _find_catcher(team_box: dict) -> int | None:
    """Walk the boxscore players dict; return mlb_id of the player with
    position abbreviation 'C' who is in the starting batting order."""
    batting_order = team_box.get("battingOrder", []) or []
    players = team_box.get("players", {}) or {}
    for pid in batting_order:
        p = players.get(f"ID{pid}", {})
        pos = (p.get("position") or {}).get("abbreviation")
        if pos == "C":
            return int(pid)
    # Fallback: scan all players for one with position C and isStarter
    for key, p in players.items():
        pos = (p.get("position") or {}).get("abbreviation")
        if pos == "C" and (p.get("gameStatus") or {}).get("isCurrentPitcher") is False:
            return _safe_int(p.get("person", {}).get("id"))
    return None


def extract_externals(payload: dict) -> dict:
    """Pull the external fields out of the gumbo payload into a flat dict
    matching the slate_game column names added in Step 9."""
    out: dict = {}
    if not payload:
        return out
    gd = payload.get("gameData") or {}
    ld = payload.get("liveData") or {}

    info = gd.get("gameInfo") or {}
    out["attendance"] = _safe_int(info.get("attendance"))

    dt = gd.get("datetime") or {}
    out["day_night"] = dt.get("dayNight")

    # weather_condition / game_duration_minutes were dropped in May 2026 —
    # see module docstring.

    venue = gd.get("venue") or {}
    out["venue_id"] = _safe_int(venue.get("id"))
    out["venue_name"] = venue.get("name")
    fi = venue.get("fieldInfo") or {}
    out["venue_capacity"] = _safe_int(fi.get("capacity"))
    out["venue_surface"] = fi.get("turfType")
    out["venue_roof_type"] = fi.get("roofType")
    out["venue_lf_line_ft"] = _safe_int(fi.get("leftLine"))
    out["venue_lf_ft"] = _safe_int(fi.get("left"))
    out["venue_lcf_ft"] = _safe_int(fi.get("leftCenter"))
    out["venue_cf_ft"] = _safe_int(fi.get("center"))
    out["venue_rcf_ft"] = _safe_int(fi.get("rightCenter"))
    out["venue_rf_ft"] = _safe_int(fi.get("right"))
    out["venue_rf_line_ft"] = _safe_int(fi.get("rightLine"))
    loc = venue.get("location") or {}
    coords = loc.get("defaultCoordinates") or {}
    out["venue_elevation_ft"] = _safe_int(loc.get("elevation"))
    out["venue_latitude"] = _safe_float(coords.get("latitude"))
    out["venue_longitude"] = _safe_float(coords.get("longitude"))
    out["venue_timezone"] = (venue.get("timeZone") or {}).get("id") or (venue.get("timeZone") or {}).get("tz")

    bx = ld.get("boxscore") or {}
    officials = bx.get("officials") or []
    ump_by_type: dict[str, dict] = {}
    for o in officials:
        ot = o.get("officialType")
        person = o.get("official") or {}
        if ot:
            ump_by_type[ot] = person
    if "Home Plate" in ump_by_type:
        out["ump_hp_id"] = _safe_int(ump_by_type["Home Plate"].get("id"))
        out["ump_hp_name"] = ump_by_type["Home Plate"].get("fullName")
    # ump_1b_id / ump_2b_id / ump_3b_id dropped in May 2026 — only HP umpire
    # has predictive lift on K-rate.

    teams = bx.get("teams") or {}
    out["home_catcher_id"] = _find_catcher(teams.get("home", {}) or {})
    out["away_catcher_id"] = _find_catcher(teams.get("away", {}) or {})

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch + parse but don't write to SQLite")
    ap.add_argument("--since", default=None,
                    help="Only re-fetch games on slate_date >= this YYYY-MM-DD")
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch even when fields are already populated")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # Find game_pks that need backfill: any slate_game where the new
        # columns are NULL (or all if --force).  Skip game_pk=0 / null.
        if args.force:
            where = "WHERE game_pk IS NOT NULL"
        else:
            where = (
                "WHERE game_pk IS NOT NULL AND venue_id IS NULL"
            )
        if args.since:
            where += f" AND slate_date >= '{args.since}'"

        cur = conn.execute(
            f"SELECT slate_date, game_pk, game_number FROM slate_game "
            f"{where} ORDER BY slate_date, game_pk"
        )
        targets = cur.fetchall()
        log.info("targets: %d (slate_date, game_pk, game_number) rows", len(targets))
        if not targets:
            log.info("nothing to backfill — re-run with --force to refresh")
            return 0

        # Each game_pk fetched once even if multiple game_numbers share it
        unique_pks = sorted({t["game_pk"] for t in targets})
        log.info("unique game_pks to fetch: %d", len(unique_pks))

        payloads: dict[int, dict] = {}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(fetch_game, pk): pk for pk in unique_pks}
            for fut in as_completed(futures):
                pk = futures[fut]
                payloads[pk] = fut.result() or {}
        log.info("fetched %d / %d in %.1fs", len(payloads), len(unique_pks), time.time() - t0)

        if args.dry_run:
            sample = next(iter(payloads.values()), None)
            if sample:
                ext = extract_externals(sample)
                log.info("sample extract: %s", json.dumps(ext, indent=2))
            return 0

        updates = 0
        skipped = 0
        for t in targets:
            payload = payloads.get(t["game_pk"])
            if not payload:
                skipped += 1
                continue
            ext = extract_externals(payload)
            if not ext:
                skipped += 1
                continue
            historical_db.update_slate_game_columns(
                conn, t["slate_date"], t["game_pk"], ext,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (skipped %d)", updates, skipped)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        # Step 3 hook: re-export CSVs so the new slate_game columns appear
        # in derived /data/ exports.
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
