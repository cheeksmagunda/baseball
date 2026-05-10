"""Backfill all team-schedule-derived features onto slate_game.

Consolidated May 2026 — merges:
    - backfill_travel_fatigue.py   (miles_72h + zones_24h)
    - backfill_schedule_flags.py   (is_getaway_day + is_day_after_night_game)

Both scripts shared the same data sources (the team-schedule cache
populated by ONE call to MLB Stats API `/schedule?teamId=` per team
per season + `venue_dim` coords).  Combining them eliminates the
duplicate cache-loading and team-id-resolution code.

All six slate_game columns produced:
  home/away_team_miles_traveled_72h    — sum of haversine distances
                                          between successive prior
                                          venues in trailing 72 hours
  home/away_team_zones_crossed_24h     — abs delta between prior-game
                                          longitude/15 and today's
                                          longitude/15
  is_getaway_day                       — 1 if EITHER team's NEXT game
                                          (within 4 days) is at a
                                          different venue
  is_day_after_night_game              — 1 if EITHER team's PREV game
                                          (within 24h) started ≥ 18:00
                                          local AND today's first pitch
                                          is < 17:00 local

Source: one MLB Stats API `/schedule?sportId=1&season=2026&teamId=N`
call per team per season (cached at scripts/output/.team_schedule_cache/).
Per-venue lat/lon comes from venue_dim.

Usage:
    python scripts/backfill_schedule_features.py
    python scripts/backfill_schedule_features.py --season 2026
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts._backfill_common import bootstrap, finalize  # noqa: E402

bootstrap("backfill-sched-feats-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".team_schedule_cache"
HEADERS = {"User-Agent": "Mozilla/5.0"}
HTTP_TIMEOUT = 30
MLB_API = "https://statsapi.mlb.com/api/v1"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_schedule_features")


# ---- Geometry helpers -------------------------------------------------------
def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    if any(v is None for v in (lat1, lon1, lat2, lon2)):
        return 0.0
    if (lat1, lon1) == (lat2, lon2):
        return 0.0
    R = 3959.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _tz_offset(lon: float | None) -> int:
    return 0 if lon is None else round(lon / 15.0)


def _local_hour(dt_utc: datetime, lon: float | None) -> int:
    return (dt_utc.hour + _tz_offset(lon)) % 24


# ---- Schedule fetch ---------------------------------------------------------
def _fetch_team_schedule(team_id: int, season: int) -> list[dict]:
    cache_file = CACHE_DIR / f"{season}_{team_id}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass
    try:
        r = requests.get(
            f"{MLB_API}/schedule",
            params={"sportId": 1, "season": season, "teamId": team_id, "hydrate": "venue"},
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        log.warning("schedule fetch failed for team %s: %s", team_id, e)
        return []
    if r.status_code != 200:
        log.warning("schedule fetch returned %s for team %s", r.status_code, team_id)
        return []
    data = r.json()
    games: list[dict] = []
    for date_obj in data.get("dates", []) or []:
        for g in date_obj.get("games", []) or []:
            games.append(g)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(games))
    return games


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        # Build venue_id → (lat, lon) map
        cur = conn.execute(
            "SELECT venue_id, venue_latitude, venue_longitude FROM venue_dim"
        )
        venue_coords: dict[int, tuple[float | None, float | None]] = {}
        for r in cur.fetchall():
            venue_coords[r["venue_id"]] = (
                float(r["venue_latitude"]) if r["venue_latitude"] is not None else None,
                float(r["venue_longitude"]) if r["venue_longitude"] is not None else None,
            )

        # Resolve team abbr → team_id from MLB API
        try:
            r = requests.get(
                f"{MLB_API}/teams", params={"sportId": 1},
                headers=HEADERS, timeout=30,
            )
            teams_data = r.json()
        except Exception as e:
            log.error("teams fetch failed: %s", e)
            return 1
        team_id_by_abbr: dict[str, int] = {}
        for tm in teams_data.get("teams") or []:
            tid = tm.get("id")
            for k in ("abbreviation", "teamCode", "fileCode"):
                v = tm.get(k)
                if v and tid:
                    team_id_by_abbr[str(v).upper()] = int(tid)

        # Build per-team chronological list of (datetime_utc, venue_id)
        cur = conn.execute(
            "SELECT DISTINCT home_team FROM slate_game UNION "
            "SELECT DISTINCT away_team FROM slate_game"
        )
        team_abbrs = sorted({r[0] for r in cur.fetchall()})
        team_history: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
        for abbr in team_abbrs:
            tid = team_id_by_abbr.get(abbr)
            if not tid:
                log.warning("no team_id for abbr %s", abbr)
                continue
            for g in _fetch_team_schedule(tid, args.season):
                if g.get("status", {}).get("abstractGameState") in ("Cancelled", "Postponed"):
                    continue
                gdt = g.get("gameDate")
                vid = (g.get("venue") or {}).get("id")
                if not gdt or not vid:
                    continue
                try:
                    dt = datetime.fromisoformat(gdt.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                team_history[abbr].append((dt, int(vid)))
            team_history[abbr].sort(key=lambda x: x[0])
        log.info("indexed schedules for %d teams", len(team_history))

        # Iterate slate_games
        if args.force:
            where = "WHERE 1=1"
        else:
            where = (
                "WHERE home_team_miles_traveled_72h IS NULL "
                "OR is_getaway_day IS NULL"
            )
        cur = conn.execute(
            f"SELECT slate_date, game_pk, datetime_utc, venue_id, "
            f"home_team, away_team FROM slate_game {where}"
        )
        targets = cur.fetchall()
        log.info("targets: %d games", len(targets))

        updates = 0
        for t in targets:
            try:
                game_dt = datetime.fromisoformat(t["datetime_utc"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            today_lat, today_lon = venue_coords.get(t["venue_id"], (None, None))
            today_local_hour = _local_hour(game_dt, today_lon)

            updates_dict: dict = {}

            # Per-team: travel features + getaway/night flags
            getaway_any = day_after_night_any = False
            for side, team in (("home", t["home_team"]), ("away", t["away_team"])):
                history = team_history.get(team, [])
                prior_72h = [
                    (dt, vid) for dt, vid in history
                    if (game_dt - dt) <= timedelta(hours=72) and dt < game_dt
                ]
                prior_24h = [(dt, vid) for dt, vid in prior_72h
                             if (game_dt - dt) <= timedelta(hours=24)]
                future = [(dt, vid) for dt, vid in history if dt > game_dt]

                # --- miles_72h ---
                miles = 0.0
                last_coords = None
                for _, vid in sorted(prior_72h, key=lambda x: x[0]):
                    coords = venue_coords.get(vid)
                    if last_coords is not None and coords is not None and coords[0] is not None:
                        miles += _haversine_miles(*last_coords, *coords)
                    if coords is not None and coords[0] is not None:
                        last_coords = coords
                if last_coords is not None and today_lat is not None:
                    miles += _haversine_miles(*last_coords, today_lat, today_lon)
                updates_dict[f"{side}_team_miles_traveled_72h"] = int(round(miles))

                # --- zones_crossed_24h ---
                zones = 0
                if prior_24h and today_lon is not None:
                    last_dt, last_vid = max(prior_24h, key=lambda x: x[0])
                    last_coords_24 = venue_coords.get(last_vid)
                    if last_coords_24 is not None and last_coords_24[1] is not None:
                        zones = abs(_tz_offset(today_lon) - _tz_offset(last_coords_24[1]))
                updates_dict[f"{side}_team_zones_crossed_24h"] = zones

                # --- is_getaway_day (set if EITHER team's next game is at
                # a different venue within 4 days) ---
                if future:
                    next_dt, next_vid = future[0]
                    if (next_dt - game_dt).total_seconds() <= 4 * 24 * 3600:
                        if next_vid != t["venue_id"]:
                            getaway_any = True

                # --- is_day_after_night_game (set if EITHER team's prev
                # game in trailing 24h started ≥ 18:00 local AND today is
                # a day game (< 17:00 local)) ---
                if today_local_hour < 17 and prior_24h:
                    prev_dt, prev_vid = max(prior_24h, key=lambda x: x[0])
                    prev_lon = venue_coords.get(prev_vid, (None, None))[1]
                    prev_local = _local_hour(prev_dt, prev_lon)
                    if prev_local >= 18:
                        day_after_night_any = True

            updates_dict["is_getaway_day"] = int(getaway_any)
            updates_dict["is_day_after_night_game"] = int(day_after_night_any)

            historical_db.update_slate_game_columns(
                conn, t["slate_date"], t["game_pk"], updates_dict,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d", updates)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(finalize(main()))
