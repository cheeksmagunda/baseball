"""Backfill air density composite (humidity + pressure + computed ratio) onto slate_game.

Phase E add (May 2026 batter sweep).  HR distance scales DIRECTLY with
air density; the existing temperature_f / wind_speed_mph cover only
two of the three drivers.  Coors plays the way it does because of low
density; humid Florida nights at sea level play smaller than the
temperature alone would suggest.

Source: Open-Meteo Archive
(`archive-api.open-meteo.com/v1/archive`) for first-pitch hour at
venue lat/lon.  Free, no API key.

Computed columns:
  humidity_pct       — relative humidity at first pitch (0-100)
  pressure_hpa       — surface pressure in hectopascals (~1013 standard)
  air_density_ratio  — actual_density / sea_level_standard_density
                       (1.00 = standard, <1.00 = thin air = ball carries)

Air density formula (simplified Tetens + ideal gas):
  T_K = (T_F − 32) × 5/9 + 273.15
  e   = 6.1078 × exp(17.27 × T_C / (T_C + 237.3)) × humidity_pct / 100   [vapor pressure, hPa]
  Pd  = pressure_hpa − e                                                  [dry-air partial pressure]
  rho = (Pd × 100 / (287.05 × T_K)) + (e × 100 / (461.495 × T_K))         [kg/m^3]
  ratio = rho / 1.225  (sea-level standard at 15°C)

Cache: scripts/output/.air_density_cache/<lat>_<lon>_<date>_<hour>.json

Usage:
    python scripts/backfill_air_density.py
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-air-density-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".air_density_cache"
HEADERS = {"User-Agent": "Mozilla/5.0"}
HTTP_TIMEOUT = 30
SEA_LEVEL_RHO = 1.225  # kg/m^3 at 15°C, 1013.25 hPa, dry air

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_air_density")


def _air_density(temp_f: float, humidity_pct: int, pressure_hpa: float) -> float:
    """Compute air density (kg/m^3) using simplified Tetens + ideal gas law."""
    t_c = (temp_f - 32) * 5.0 / 9.0
    t_k = t_c + 273.15
    # Saturation vapor pressure (hPa) via Tetens
    es = 6.1078 * math.exp(17.27 * t_c / (t_c + 237.3))
    e = es * (humidity_pct / 100.0)
    pd = pressure_hpa - e  # dry-air partial pressure
    rho = (pd * 100 / (287.05 * t_k)) + (e * 100 / (461.495 * t_k))
    return rho


def _fetch_hour(lat: float, lon: float, date_iso: str, hour: int) -> dict | None:
    """Fetch hourly humidity + pressure for a venue at a specific date/hour.

    Open-Meteo archive returns full-day hourly arrays; we slice to `hour`.
    """
    cache_file = CACHE_DIR / f"{round(lat, 3)}_{round(lon, 3)}_{date_iso}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            data = None
    else:
        data = None
    if data is None:
        try:
            r = requests.get(
                "https://archive-api.open-meteo.com/v1/archive",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "start_date": date_iso,
                    "end_date": date_iso,
                    "hourly": "temperature_2m,relative_humidity_2m,surface_pressure",
                    "temperature_unit": "fahrenheit",
                },
                headers=HEADERS,
                timeout=HTTP_TIMEOUT,
            )
        except Exception as e:
            log.warning("fetch failed for %s,%s %s: %s", lat, lon, date_iso, e)
            return None
        if r.status_code != 200:
            log.warning("fetch returned %s for %s,%s %s", r.status_code, lat, lon, date_iso)
            return None
        data = r.json()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data))

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    target_iso = f"{date_iso}T{hour:02d}:00"
    try:
        idx = times.index(target_iso)
    except ValueError:
        return None
    temp_f = (hourly.get("temperature_2m") or [])[idx]
    humidity = (hourly.get("relative_humidity_2m") or [])[idx]
    pressure = (hourly.get("surface_pressure") or [])[idx]
    if temp_f is None or humidity is None or pressure is None:
        return None
    return {
        "temperature_f": float(temp_f),
        "humidity_pct": int(round(humidity)),
        "pressure_hpa": round(float(pressure), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE datetime_utc IS NOT NULL"
        else:
            where = "WHERE datetime_utc IS NOT NULL AND humidity_pct IS NULL"
        cur = conn.execute(
            f"""
            SELECT sg.slate_date, sg.game_pk, sg.datetime_utc,
                   vd.venue_latitude, vd.venue_longitude
            FROM slate_game sg
            JOIN venue_dim vd ON vd.venue_id = sg.venue_id
            {where}
            ORDER BY sg.slate_date, sg.game_pk
            """
        )
        targets = cur.fetchall()
        log.info("targets: %d games", len(targets))

        updates = 0
        misses = 0
        for t in targets:
            if t["venue_latitude"] is None or t["venue_longitude"] is None:
                misses += 1
                continue
            try:
                dt = datetime.fromisoformat(t["datetime_utc"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                misses += 1
                continue
            date_iso = dt.date().isoformat()
            rec = _fetch_hour(
                float(t["venue_latitude"]), float(t["venue_longitude"]),
                date_iso, dt.hour,
            )
            if rec is None:
                misses += 1
                continue
            rho = _air_density(rec["temperature_f"], rec["humidity_pct"], rec["pressure_hpa"])
            updates_dict = {
                "humidity_pct": rec["humidity_pct"],
                "pressure_hpa": rec["pressure_hpa"],
                "air_density_ratio": round(rho / SEA_LEVEL_RHO, 4),
            }
            historical_db.update_slate_game_columns(
                conn, t["slate_date"], t["game_pk"], updates_dict,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no archive data: %d)", updates, misses)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0 and not os.environ.get("HISTORICAL_DB"):
        from scripts.export_historical_csvs import export_all
        export_all()
    sys.exit(rc)
