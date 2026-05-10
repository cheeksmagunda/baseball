"""Backfill actual weather at first pitch onto slate_game from Open-Meteo's
historical Archive API.

The pre-existing temperature_f / wind_speed_mph / wind_direction* columns
are the T-65 *forecast* captured at slate enrichment time.  These new
columns are the *actual* readings at the venue's lat/lon at the first-
pitch hour, sourced from Open-Meteo Archive (5-day lag).

External (no derivations) — Archive returns hourly temperature_2m,
wind_speed_10m, wind_direction_10m, precipitation, relative_humidity_2m,
pressure_msl, cloud_cover at the venue coordinate.  We pick the hour
closest to first pitch and store the seven values verbatim.

Pre-condition: venue_latitude / venue_longitude must already be
populated (Step 9: backfill_game_externals.py).

Usage:
    python scripts/backfill_weather_actuals.py
    python scripts/backfill_weather_actuals.py --force
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-weather-actuals-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".weather_actuals_cache"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HTTP_TIMEOUT = 30
MAX_WORKERS = 6   # be polite to Open-Meteo's free tier

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_weather_actuals")


def _safe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_archive(lat: float, lon: float, slate_date: str) -> dict | None:
    """Fetch one date's hourly archive at (lat, lon).  Cache by
    (lat, lon, slate_date) tuple."""
    key = f"{lat:.4f}_{lon:.4f}_{slate_date}"
    cache_file = CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass
    try:
        r = requests.get(
            ARCHIVE_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "start_date": slate_date,
                "end_date": slate_date,
                "hourly": (
                    "temperature_2m,wind_speed_10m,wind_direction_10m,"
                    "precipitation,relative_humidity_2m,pressure_msl,cloud_cover"
                ),
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "mm",
            },
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("archive fetch failed lat=%s lon=%s date=%s: %s",
                    lat, lon, slate_date, e)
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(r.text)
    return r.json()


def extract_actuals(payload: dict, target_utc_hour: int) -> dict:
    if not payload:
        return {}
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    idx = next((i for i, t in enumerate(times)
                if t.endswith(f"T{target_utc_hour:02d}:00")), None)
    if idx is None:
        return {}

    def at(key):
        arr = hourly.get(key) or []
        return arr[idx] if idx < len(arr) else None

    return {
        "actual_temperature_f": _safe_float(at("temperature_2m")),
        "actual_wind_speed_mph": _safe_float(at("wind_speed_10m")),
        "actual_wind_direction_deg": _safe_int(at("wind_direction_10m")),
        "actual_precipitation_mm": _safe_float(at("precipitation")),
        "actual_humidity_pct": _safe_int(at("relative_humidity_2m")),
        "actual_pressure_hpa": _safe_float(at("pressure_msl")),
        "actual_cloud_cover_pct": _safe_int(at("cloud_cover")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        if args.force:
            where = "WHERE venue_latitude IS NOT NULL AND venue_longitude IS NOT NULL AND datetime_utc IS NOT NULL"
        else:
            where = (
                "WHERE venue_latitude IS NOT NULL AND venue_longitude IS NOT NULL "
                "AND datetime_utc IS NOT NULL "
                "AND actual_temperature_f IS NULL"
            )
        cur = conn.execute(
            f"SELECT slate_date, game_pk, game_number, venue_latitude, "
            f"venue_longitude, datetime_utc FROM slate_game {where} "
            f"ORDER BY slate_date, game_pk"
        )
        targets = cur.fetchall()
        log.info("targets: %d", len(targets))
        if not targets:
            log.info("nothing to backfill — re-run with --force to refresh")
            return 0

        # Bucket by (lat, lon, date) so we hit Open-Meteo once per (venue, day).
        buckets: dict[tuple[float, float, str], list] = {}
        for t in targets:
            key = (round(t["venue_latitude"], 4),
                   round(t["venue_longitude"], 4),
                   t["slate_date"])
            buckets.setdefault(key, []).append(t)
        log.info("unique (venue, date) buckets to fetch: %d", len(buckets))

        payloads: dict[tuple, dict] = {}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {
                ex.submit(fetch_archive, k[0], k[1], k[2]): k
                for k in buckets
            }
            for fut in as_completed(futures):
                key = futures[fut]
                payloads[key] = fut.result() or {}
        log.info("fetched %d / %d in %.1fs",
                 len(payloads), len(buckets), time.time() - t0)

        if args.dry_run:
            sample_key = next(iter(payloads), None)
            if sample_key:
                log.info("sample: lat=%s lon=%s date=%s",
                         sample_key[0], sample_key[1], sample_key[2])
                # Pick a sample target hour from that bucket
                t = buckets[sample_key][0]
                hour = int(datetime.fromisoformat(
                    t["datetime_utc"].replace("Z", "+00:00")
                ).strftime("%H"))
                log.info("sample extract: %s",
                         json.dumps(extract_actuals(payloads[sample_key], hour), indent=2))
            return 0

        updates = 0
        skipped = 0
        for key, items in buckets.items():
            payload = payloads.get(key)
            if not payload:
                skipped += len(items)
                continue
            for t in items:
                hour = int(datetime.fromisoformat(
                    t["datetime_utc"].replace("Z", "+00:00")
                ).strftime("%H"))
                ext = extract_actuals(payload, hour)
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
