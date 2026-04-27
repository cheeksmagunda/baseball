"""Backfill historical_slate_results.json with pre-game environmental conditions.

For every game in /data/historical_slate_results.json, populates the env fields
the scoring/filter pipeline reads at T-65:
  - Vegas      (vegas_total, home_moneyline, away_moneyline) — from realsports.io
  - Records    (home_team_record_w/l, away_team_record_w/l)  — from realsports.io
  - GamePk     (MLB Stats API gamePk) — from MLB schedule
  - Pitchers   (home/away_starter_{id,name,hand,era,k_per_9,whip}) — MLB Stats API
  - Team stats (home/away_team_ops, *_team_k_pct, *_bullpen_era) — MLB Stats API
  - Park       (park_team, park_hr_factor) — constants table
  - Weather    (temperature_f, wind_speed_mph, wind_direction[_deg]) — Open-Meteo archive
  - Momentum   (home/away_l10_wins, *_series_wins) — computed from MLB schedule

Idempotent: games with `vegas_total` already populated are skipped (use --force
to re-fetch).

Pitcher and team stats use CURRENT cumulative season values (not as-of-game-date).
For the 28-day history window this is a reasonable approximation; a stricter
as-of-date pass would aggregate per-game logs and is left as future work.

Sources:
  - realsports.io platform API   (Vegas + team records — direct HTTP with the
                                  scraper's stored auth)
  - MLB Stats API                (free, no key)
  - Open-Meteo archive endpoint  (free, no key, 5-day lag)

Usage:
    .venv-scraper/bin/python scripts/backfill_slate_env_conditions.py
    .venv-scraper/bin/python scripts/backfill_slate_env_conditions.py --date 2026-04-19
    .venv-scraper/bin/python scripts/backfill_slate_env_conditions.py --force
"""
import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SLATE_RESULTS = DATA_DIR / "historical_slate_results.json"
STORAGE_STATE = ROOT / "scraper" / "storage_state.json"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_env")

# -- Constants imported as plain values to avoid app/ deps in this script ----
SEASON = 2026

# Standard MLB team key normalization — keep in sync with app.core.constants.
TEAM_ABBR_ALIASES = {
    "KCR": "KC", "CHW": "CWS", "AZ": "ARI", "WSN": "WSH",
    "TBR": "TB", "SDP": "SD", "SFG": "SF", "OAK": "ATH",
}


def canon(team: str) -> str:
    return TEAM_ABBR_ALIASES.get(team.strip().upper(), team.strip().upper())


# Park HR factors (from app/core/constants.py) — keys are canonical 3-letter codes.
PARK_HR_FACTORS = {
    "COL": 1.38, "CIN": 1.18, "PHI": 1.12, "HOU": 1.10, "TEX": 1.08,
    "CHC": 1.06, "BAL": 1.05, "TOR": 1.04, "NYY": 1.03, "BOS": 1.02,
    "MIL": 1.02, "MIN": 1.01, "ATL": 1.00, "CLE": 1.00, "DET": 0.99,
    "ARI": 0.98, "STL": 0.98, "CWS": 0.97, "WSH": 0.97, "KC":  0.96,
    "PIT": 0.96, "LAA": 0.95, "NYM": 0.95, "TB":  0.94, "SD":  0.93,
    "SF":  0.92, "SEA": 0.91, "MIA": 0.90, "ATH": 1.09, "LAD": 0.89,
}

# Stadium coordinates (from app/core/open_meteo.py) — for weather lookups.
STADIUM_COORDS = {
    "ARI": (33.445, -112.067), "ATL": (33.891,  -84.468),
    "BAL": (39.284,  -76.622), "BOS": (42.347,  -71.097),
    "CHC": (41.948,  -87.656), "CIN": (39.097,  -84.507),
    "CLE": (41.496,  -81.685), "COL": (39.756, -104.994),
    "CWS": (41.830,  -87.634), "DET": (42.339,  -83.049),
    "HOU": (29.757,  -95.355), "KC":  (39.051,  -94.480),
    "LAA": (33.800, -117.883), "LAD": (34.074, -118.240),
    "MIA": (25.778,  -80.220), "MIL": (43.028,  -88.097),
    "MIN": (44.982,  -93.278), "NYM": (40.757,  -73.846),
    "NYY": (40.829,  -73.926), "ATH": (38.580, -121.500),
    "PHI": (39.906,  -75.166), "PIT": (40.447,  -80.006),
    "SD":  (32.707, -117.157), "SF":  (37.778, -122.389),
    "SEA": (47.591, -122.332), "STL": (38.623,  -90.193),
    "TB":  (27.768,  -82.654), "TEX": (32.751,  -97.083),
    "TOR": (43.641,  -79.389), "WSH": (38.873,  -77.008),
}

# Wind-out bearing for outdoor parks (from app/core/open_meteo.py).
STADIUM_WIND_OUT_DEG = {
    "ATL": 310, "BAL": 315, "BOS": 315, "CHC": 220, "CIN": 230,
    "CLE": 135, "COL": 160, "CWS": 175, "DET": 160, "KC":  170,
    "LAA": 200, "LAD": 130, "MIN": 315, "NYM": 205, "NYY": 175,
    "ATH": 170, "PHI": 205, "PIT": 165, "SD":  170, "SF":  150,
    "STL": 315, "WSH": 215,
}

# MLB team IDs (from app/core/mlb_api.py).
TEAM_MLB_IDS = {
    "ARI": 109, "ATL": 144, "BAL": 110, "BOS": 111, "CHC": 112,
    "CWS": 145, "CIN": 113, "CLE": 114, "COL": 115, "DET": 116,
    "HOU": 117, "KC":  118, "LAA": 108, "LAD": 119, "MIA": 146,
    "MIL": 158, "MIN": 142, "NYM": 121, "NYY": 147, "ATH": 133,
    "PHI": 143, "PIT": 134, "SD":  135, "SF":  137, "SEA": 136,
    "STL": 138, "TB":  139, "TEX": 140, "TOR": 141, "WSH": 120,
}

MLB_API = "https://statsapi.mlb.com/api/v1"
REALSPORTS_API = "https://web.realapp.com"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

HTTP_TIMEOUT = 20
MAX_WORKERS = 10
WEATHER_MAX_WORKERS = 3  # Open-Meteo throttles bursts; keep weather concurrency low


# ---------------------------------------------------------------------------
# Auth — load real-auth-info from storage_state and capture a fresh
# real-request-token via a single short Playwright session.
# ---------------------------------------------------------------------------

def _load_real_auth_info() -> str:
    state = json.loads(STORAGE_STATE.read_text())
    for orig in state.get("origins", []):
        for ls in orig.get("localStorage", []):
            if ls.get("name") == "e-accounts":
                accounts = json.loads(ls["value"])
                if accounts:
                    a = accounts[0]["authInfo"]
                    return f"{a['userId']}!{a['deviceId']}!{a['token']}"
    raise RuntimeError("could not extract real-auth-info from storage_state")


def _capture_request_token() -> dict:
    """Fire up Playwright briefly to capture a fresh real-request-token + headers."""
    captured: dict = {}

    def on_req(req):
        if "web.realapp.com" in req.url and "/home/mlb/day" in req.url and not captured:
            captured.update(dict(req.headers))

    log.info("Capturing realsports request-token via Playwright (one-time) ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 599, "height": 868},
            storage_state=str(STORAGE_STATE),
        )
        page = ctx.new_page()
        page.on("request", on_req)
        page.goto("https://realsports.io/", wait_until="networkidle", timeout=30000)
        time.sleep(2)
        page.get_by_text("MLB", exact=True).first.click(timeout=15000)
        time.sleep(2)
        page.wait_for_load_state("networkidle", timeout=10000)
        # Click any visible date — we only need the headers, not the data
        for label_offset in range(0, 7):
            label = (datetime.now(timezone.utc) - timedelta(days=label_offset)).strftime("%b %-d")
            try:
                page.get_by_text(label, exact=True).first.click(timeout=8000)
                break
            except Exception:
                continue
        time.sleep(2)
        browser.close()

    if not captured:
        raise RuntimeError("failed to capture realsports request headers")
    log.info(f"  captured {len(captured)} headers")
    return captured


# ---------------------------------------------------------------------------
# Realsports API — direct HTTP, no Playwright per call.
# ---------------------------------------------------------------------------

def fetch_realsports_games(date: str, hdrs: dict) -> list[dict]:
    """Fetch the daily slate from realsports for the given YYYY-MM-DD."""
    r = requests.get(
        f"{REALSPORTS_API}/home/mlb/day/next",
        params={"cohort": 0, "day": date},
        headers=hdrs,
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    games = r.json().get("content", {}).get("games", [])
    out = []
    for g in games:
        out.append({
            "id": g.get("id"),
            "datetime_utc": g.get("dateTime"),
            "home": canon(g.get("homeTeamKey", "")),
            "away": canon(g.get("awayTeamKey", "")),
            "home_team_id": g.get("homeTeamId"),
            "away_team_id": g.get("awayTeamId"),
            "vegas_total": float(g["overUnder"]) if g.get("overUnder") else None,
            "home_moneyline": g.get("homeMoneyline"),
            "away_moneyline": g.get("awayMoneyline"),
            "home_team_record_w": g.get("homeWins"),
            "home_team_record_l": g.get("homeLosses"),
            "away_team_record_w": g.get("awayWins"),
            "away_team_record_l": g.get("awayLosses"),
        })
    return out


# ---------------------------------------------------------------------------
# MLB Stats API helpers (sync via requests).
# ---------------------------------------------------------------------------

def fetch_mlb_schedule(date: str) -> list[dict]:
    """For each game on the date, return: gamePk, home/away abbr, probable pitcher info."""
    r = requests.get(
        f"{MLB_API}/schedule",
        params={"date": date, "sportId": 1, "hydrate": "probablePitcher,team,linescore"},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for d in r.json().get("dates", []):
        for g in d.get("games", []):
            teams = g.get("teams", {})
            h = teams.get("home", {})
            a = teams.get("away", {})
            ht = h.get("team", {}); at = a.get("team", {})
            hp = h.get("probablePitcher") or {}
            ap = a.get("probablePitcher") or {}
            out.append({
                "game_pk": g.get("gamePk"),
                "home": canon(ht.get("abbreviation", "")),
                "away": canon(at.get("abbreviation", "")),
                "home_starter_id": hp.get("id"),
                "home_starter_name": hp.get("fullName"),
                "home_starter_hand": (hp.get("pitchHand") or {}).get("code"),
                "away_starter_id": ap.get("id"),
                "away_starter_name": ap.get("fullName"),
                "away_starter_hand": (ap.get("pitchHand") or {}).get("code"),
                "datetime_utc": g.get("gameDate"),
            })
    return out


def fetch_pitcher_season_stats(mlb_id: int) -> dict:
    """Return {era, k_per_9, whip} for a pitcher's current season aggregate.

    Note: returns CURRENT cumulative stats (post-script-run), not as-of-game-date.
    """
    if not mlb_id:
        return {"era": None, "k_per_9": None, "whip": None}
    r = requests.get(
        f"{MLB_API}/people/{mlb_id}",
        params={
            "hydrate": f"stats(group=[pitching],type=[season],season={SEASON})",
        },
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    people = r.json().get("people", [])
    if not people:
        return {"era": None, "k_per_9": None, "whip": None}
    stats_groups = people[0].get("stats", [])
    for sg in stats_groups:
        if sg.get("group", {}).get("displayName") == "pitching":
            splits = sg.get("splits", [])
            if not splits:
                continue
            stat = splits[0].get("stat", {})
            era = _safe_float(stat.get("era"))
            whip = _safe_float(stat.get("whip"))
            ip = _safe_float(stat.get("inningsPitched"))
            so = _safe_float(stat.get("strikeOuts"))
            k_per_9 = (so / ip * 9.0) if (ip and ip > 0 and so is not None) else None
            return {"era": era, "k_per_9": k_per_9, "whip": whip}
    return {"era": None, "k_per_9": None, "whip": None}


def fetch_team_batting_stats(team_id: int) -> dict:
    """Return {ops, k_pct} for a team's current season hitting aggregate."""
    r = requests.get(
        f"{MLB_API}/teams/{team_id}/stats",
        params={"stats": "season", "group": "hitting", "season": SEASON},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    stats = r.json().get("stats", [])
    for sg in stats:
        splits = sg.get("splits", [])
        if not splits:
            continue
        s = splits[0].get("stat", {})
        ops = _safe_float(s.get("ops"))
        pa = _safe_float(s.get("plateAppearances"))
        so = _safe_float(s.get("strikeOuts"))
        k_pct = (so / pa) if (pa and pa > 0 and so is not None) else None
        return {"ops": ops, "k_pct": k_pct}
    return {"ops": None, "k_pct": None}


def fetch_team_pitching_stats(team_id: int) -> dict:
    """Return {bullpen_era} ≈ team season pitching ERA.

    True bullpen-only ERA would require splitting by SP vs RP; the MLB API
    doesn't expose that on the team-aggregate endpoint without per-game logs.
    Team season pitching ERA is used as a proxy for bullpen quality, matching
    the existing live pipeline behavior in app/services/data_collection.py.
    """
    r = requests.get(
        f"{MLB_API}/teams/{team_id}/stats",
        params={"stats": "season", "group": "pitching", "season": SEASON},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    stats = r.json().get("stats", [])
    for sg in stats:
        splits = sg.get("splits", [])
        if not splits:
            continue
        s = splits[0].get("stat", {})
        return {"bullpen_era": _safe_float(s.get("era"))}
    return {"bullpen_era": None}


def fetch_team_full_schedule(team_id: int) -> list[dict]:
    """Return all completed games for a team this season, sorted by date.

    Each entry: {date, opponent, home, win, score_for, score_against}
    """
    r = requests.get(
        f"{MLB_API}/schedule",
        params={
            "teamId": team_id,
            "sportId": 1,
            "season": SEASON,
            "startDate": f"{SEASON}-03-01",
            "endDate": f"{SEASON}-11-15",
            "hydrate": "team",
        },
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for d in r.json().get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            teams = g.get("teams", {})
            h = teams.get("home", {})
            a = teams.get("away", {})
            ht = h.get("team", {}); at = a.get("team", {})
            home_id = ht.get("id"); away_id = at.get("id")
            home_score = h.get("score"); away_score = a.get("score")
            if home_score is None or away_score is None:
                continue
            is_home = (home_id == team_id)
            opp_id = away_id if is_home else home_id
            opp_abbr = canon((at if is_home else ht).get("abbreviation", ""))
            score_for = home_score if is_home else away_score
            score_against = away_score if is_home else home_score
            out.append({
                "date": g.get("officialDate") or g.get("gameDate", "")[:10],
                "opp_id": opp_id,
                "opp_abbr": opp_abbr,
                "is_home": is_home,
                "score_for": score_for,
                "score_against": score_against,
                "win": score_for > score_against,
            })
    out.sort(key=lambda x: x["date"])
    return out


def fetch_weather_archive(park_team: str, game_date: str, game_utc_hour: int) -> dict:
    """Fetch hourly weather from Open-Meteo's archive endpoint for the given park/hour.

    Retries on 429 (rate-limit) with exponential backoff. Open-Meteo's free tier
    is generous but bursts can trigger throttling; we cap concurrency upstream
    and back off on the rare 429.
    """
    coords = STADIUM_COORDS.get(park_team)
    if not coords:
        return {"temperature_f": None, "wind_speed_mph": None,
                "wind_direction": None, "wind_direction_deg": None}
    lat, lon = coords
    last_exc = None
    for attempt in range(4):
        try:
            r = requests.get(
                OPEN_METEO_ARCHIVE,
                params={
                    "latitude": lat, "longitude": lon,
                    "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m",
                    "temperature_unit": "celsius", "wind_speed_unit": "kmh",
                    "timezone": "UTC",
                    "start_date": game_date, "end_date": game_date,
                },
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    else:
        if last_exc:
            raise last_exc
    h = r.json().get("hourly", {})
    times = h.get("time", [])
    temps = h.get("temperature_2m", [])
    speeds = h.get("wind_speed_10m", [])
    dirs = h.get("wind_direction_10m", [])
    if not times:
        return {"temperature_f": None, "wind_speed_mph": None,
                "wind_direction": None, "wind_direction_deg": None}
    best_idx, best_diff = 0, 999
    for i, t in enumerate(times):
        try:
            hr = int(t.split("T")[1][:2])
            diff = abs(hr - game_utc_hour)
            if diff < best_diff:
                best_diff = diff; best_idx = i
        except Exception:
            continue
    temp_c = temps[best_idx] if best_idx < len(temps) else None
    speed_kmh = speeds[best_idx] if best_idx < len(speeds) else None
    dir_deg = dirs[best_idx] if best_idx < len(dirs) else None
    if temp_c is None or speed_kmh is None or dir_deg is None:
        return {"temperature_f": None, "wind_speed_mph": None,
                "wind_direction": None, "wind_direction_deg": None}
    return {
        "temperature_f": round(temp_c * 9 / 5 + 32),
        "wind_speed_mph": round(speed_kmh * 0.621371, 1),
        "wind_direction": _classify_wind(round(dir_deg), park_team),
        "wind_direction_deg": round(dir_deg),
    }


# ---------------------------------------------------------------------------
# Series + L10 derivation (from a team's full season schedule).
# ---------------------------------------------------------------------------

def compute_l10_and_series(
    schedule: list[dict],
    target_date: str,
    opp_abbr: str,
    is_home: bool,
) -> dict:
    """Return {l10_wins, series_wins} for a team going INTO target_date.

    L10: wins in the last 10 completed games strictly before target_date.
    series_wins: wins against opp_abbr in the most recent consecutive
                 same-opponent stretch ending strictly before target_date.
    """
    prior = [g for g in schedule if g["date"] < target_date]
    last10 = prior[-10:]
    l10_wins = sum(1 for g in last10 if g["win"])

    series_wins = 0
    for g in reversed(prior):
        if g["opp_abbr"] != opp_abbr or g["is_home"] != is_home:
            break
        if g["win"]:
            series_wins += 1
    return {"l10_wins": l10_wins, "series_wins": series_wins}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _classify_wind(deg: int, park_team: str) -> str:
    """Return 'OUT', 'IN', or 8-point compass label."""
    center = STADIUM_WIND_OUT_DEG.get(park_team)
    if center is not None:
        diff_out = abs((deg - center + 180) % 360 - 180)
        if diff_out <= 45:
            return "OUT"
        diff_in = abs((deg - (center + 180) % 360 + 180) % 360 - 180)
        if diff_in <= 45:
            return "IN"
    idx = int((deg % 360 + 22.5) / 45) % 8
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[idx]


# ---------------------------------------------------------------------------
# Main backfill loop
# ---------------------------------------------------------------------------

def run(target_date: str | None = None, force: bool = False):
    envelopes = json.loads(SLATE_RESULTS.read_text())
    log.info(f"Loaded {len(envelopes)} slate envelopes from {SLATE_RESULTS.name}")

    # Pre-step: capture realsports auth headers.
    auth_info = _load_real_auth_info()
    rs_hdrs = _capture_request_token()
    rs_hdrs["real-auth-info"] = auth_info  # ensure freshness

    # Caches keyed by id — we never re-fetch.
    pitcher_cache: dict[int, dict] = {}
    team_batting_cache: dict[int, dict] = {}
    team_pitching_cache: dict[int, dict] = {}
    team_schedule_cache: dict[int, list[dict]] = {}

    # Helper: get-or-fetch from a cache.
    def cached(cache: dict, key, fetcher):
        if key in cache:
            return cache[key]
        try:
            cache[key] = fetcher(key)
        except Exception as e:
            log.warning(f"  fetch failed for key={key}: {e}")
            cache[key] = {} if isinstance(cache, dict) else None
        return cache[key]

    dates_to_process = [e for e in envelopes if not target_date or e["date"] == target_date]
    log.info(f"Processing {len(dates_to_process)} date(s)")

    # Phase 1: pre-fetch team stats + schedules in parallel (one-time).
    log.info("Phase 1: fetching all 30 teams' batting / pitching / schedule data ...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {}
        for abbr, tid in TEAM_MLB_IDS.items():
            futures[ex.submit(fetch_team_batting_stats, tid)] = ("bat", tid, abbr)
            futures[ex.submit(fetch_team_pitching_stats, tid)] = ("pit", tid, abbr)
            futures[ex.submit(fetch_team_full_schedule, tid)] = ("sch", tid, abbr)
        for f in as_completed(futures):
            kind, tid, abbr = futures[f]
            try:
                res = f.result()
            except Exception as e:
                log.warning(f"  {kind} {abbr} (id={tid}) failed: {e}")
                res = [] if kind == "sch" else {}
            if kind == "bat":
                team_batting_cache[tid] = res
            elif kind == "pit":
                team_pitching_cache[tid] = res
            elif kind == "sch":
                team_schedule_cache[tid] = res
    log.info(f"  team caches: {len(team_batting_cache)} bat, "
             f"{len(team_pitching_cache)} pit, {len(team_schedule_cache)} sch")

    # Phase 2: process each date.
    for env in dates_to_process:
        date = env["date"]
        games = env.get("games") or []
        if not games:
            log.warning(f"{date}: no games in envelope — skipping")
            continue
        if not force and games[0].get("vegas_total") is not None:
            log.info(f"{date}: already enriched (vegas_total set on game 0) — skipping; --force to re-fetch")
            continue
        log.info(f"--- {date}: {len(games)} games ---")

        # 2a. Realsports daily payload (Vegas + records).
        try:
            rs_games = fetch_realsports_games(date, rs_hdrs)
        except Exception as e:
            log.error(f"{date}: realsports fetch failed: {e}")
            continue
        rs_by_pair = {(g["away"], g["home"]): g for g in rs_games}

        # 2b. MLB schedule (gamePk + probable pitchers).
        try:
            mlb_games = fetch_mlb_schedule(date)
        except Exception as e:
            log.error(f"{date}: MLB schedule fetch failed: {e}")
            continue
        mlb_by_pair = {(g["away"], g["home"]): g for g in mlb_games}

        # 2c. Walk our envelope's games and enrich each.
        for g in games:
            ah, ho = canon(g["away"]), canon(g["home"])
            key = (ah, ho)
            rs = rs_by_pair.get(key) or {}
            mlb = mlb_by_pair.get(key) or {}

            # Detect REVERSED home/away in our envelope: same teams, flipped
            # orientation. Both realsports and MLB API agree on which team is
            # home, so trust them and rewrite our envelope's home/away + scores
            # to match. This is a data-quality cleanup for older manual-ingest
            # rows where home/away got swapped.
            if not rs and not mlb:
                flipped_key = (ho, ah)  # try opposite orientation
                rs = rs_by_pair.get(flipped_key) or {}
                mlb = mlb_by_pair.get(flipped_key) or {}
                if rs or mlb:
                    log.info(
                        f"  {date} {ah}@{ho}: REVERSED home/away in envelope "
                        f"— rewriting to {ho}@{ah} (truth from realsports/MLB API)"
                    )
                    g["home"], g["away"] = g["away"], g["home"]
                    g["home_score"], g["away_score"] = g["away_score"], g["home_score"]
                    # winner/loser already record the actual outcome — unchanged
                    ah, ho = canon(g["away"]), canon(g["home"])

            if not rs and not mlb:
                log.warning(f"  {date} {ah}@{ho}: no realsports OR MLB match — leaving env null")

            # Vegas + records
            g["vegas_total"] = rs.get("vegas_total")
            g["home_moneyline"] = rs.get("home_moneyline")
            g["away_moneyline"] = rs.get("away_moneyline")
            g["home_team_record_w"] = rs.get("home_team_record_w")
            g["home_team_record_l"] = rs.get("home_team_record_l")
            g["away_team_record_w"] = rs.get("away_team_record_w")
            g["away_team_record_l"] = rs.get("away_team_record_l")

            # GamePk + datetime
            g["game_pk"] = mlb.get("game_pk")
            g["datetime_utc"] = mlb.get("datetime_utc") or rs.get("datetime_utc")

            # Probable pitchers
            for side in ("home", "away"):
                sid = mlb.get(f"{side}_starter_id")
                g[f"{side}_starter_id"] = sid
                g[f"{side}_starter_name"] = mlb.get(f"{side}_starter_name")
                g[f"{side}_starter_hand"] = mlb.get(f"{side}_starter_hand")
                if sid:
                    stats = cached(pitcher_cache, sid, fetch_pitcher_season_stats)
                    g[f"{side}_starter_era"] = stats.get("era")
                    g[f"{side}_starter_k_per_9"] = (
                        round(stats["k_per_9"], 2) if stats.get("k_per_9") is not None else None
                    )
                    g[f"{side}_starter_whip"] = stats.get("whip")
                else:
                    g[f"{side}_starter_era"] = None
                    g[f"{side}_starter_k_per_9"] = None
                    g[f"{side}_starter_whip"] = None

            # Team batting / bullpen
            for side, abbr in (("home", ho), ("away", ah)):
                tid = TEAM_MLB_IDS.get(abbr)
                if tid:
                    bat = team_batting_cache.get(tid, {})
                    pit = team_pitching_cache.get(tid, {})
                    g[f"{side}_team_ops"] = bat.get("ops")
                    g[f"{side}_team_k_pct"] = (
                        round(bat["k_pct"], 4) if bat.get("k_pct") is not None else None
                    )
                    g[f"{side}_bullpen_era"] = pit.get("bullpen_era")
                else:
                    log.warning(f"  unknown team abbr {abbr!r}; team stats blank")
                    g[f"{side}_team_ops"] = None
                    g[f"{side}_team_k_pct"] = None
                    g[f"{side}_bullpen_era"] = None

            # Park
            g["park_team"] = ho
            g["park_hr_factor"] = PARK_HR_FACTORS.get(ho)

            # Series + L10 (computed from team schedule cache)
            for side, abbr, opp in (("home", ho, ah), ("away", ah, ho)):
                tid = TEAM_MLB_IDS.get(abbr)
                if tid and tid in team_schedule_cache:
                    is_home = (side == "home")
                    sched = team_schedule_cache[tid]
                    s = compute_l10_and_series(sched, date, opp, is_home)
                    g[f"{side}_l10_wins"] = s["l10_wins"]
                    g[f"{side}_series_wins"] = s["series_wins"]
                else:
                    g[f"{side}_l10_wins"] = None
                    g[f"{side}_series_wins"] = None

        # 2d. Weather — parallel per game, indexed by park + UTC hour.
        weather_tasks = []
        for g in games:
            dt = g.get("datetime_utc")
            if not dt or not g.get("park_team"):
                continue
            try:
                hour = int(dt[11:13])
            except (ValueError, IndexError):
                continue
            weather_tasks.append((g, g["park_team"], date, hour))

        if weather_tasks:
            with ThreadPoolExecutor(max_workers=WEATHER_MAX_WORKERS) as ex:
                futs = {
                    ex.submit(fetch_weather_archive, park, gd, hr): g
                    for (g, park, gd, hr) in weather_tasks
                }
                for f in as_completed(futs):
                    g = futs[f]
                    try:
                        w = f.result()
                    except Exception as e:
                        log.warning(f"  weather fetch failed for {g['away']}@{g['home']}: {e}")
                        w = {"temperature_f": None, "wind_speed_mph": None,
                             "wind_direction": None, "wind_direction_deg": None}
                    g.update(w)

        log.info(f"  {date}: enriched {len(games)} games "
                 f"(pitcher_cache={len(pitcher_cache)} unique IDs)")

    # Save
    SLATE_RESULTS.write_text(json.dumps(envelopes, indent=2))
    log.info(f"Wrote {SLATE_RESULTS}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="restrict to a single YYYY-MM-DD slate")
    ap.add_argument("--force", action="store_true",
                    help="re-fetch even if vegas_total already populated")
    args = ap.parse_args()
    run(target_date=args.date, force=args.force)


if __name__ == "__main__":
    main()
