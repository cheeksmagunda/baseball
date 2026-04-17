"""Open-Meteo weather API client — free, no API key required.

Endpoints:
  Forecast: https://api.open-meteo.com/v1/forecast
  Archive:  https://archive-api.open-meteo.com/v1/archive  (5-day lag)

Fetches temperature and wind for a specific lat/lon and hour.  The caller
is responsible for choosing the right endpoint (forecast vs. archive) based
on whether the game date is in the past.

Wind direction is classified as "OUT", "IN", or a raw 8-point compass label
("N", "NE", "E", "SE", "S", "SW", "W", "NW") depending on whether the park
is in STADIUM_WIND_OUT_FROM_DEG.  The "OUT" label is the only one that
triggers the BATTER_ENV_WIND_OUT_BONUS in filter_strategy.py; all others
are stored as-is for observability.
"""
import logging
import math
from datetime import date

import httpx

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"
_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Stadium coordinates — (latitude, longitude) for each MLB park
# Used to pick the right weather grid point from Open-Meteo.
# Retractable-roof parks are included so we can still capture temperature;
# wind bonus logic is suppressed for those parks via STADIUM_WIND_OUT_FROM_DEG.
# ---------------------------------------------------------------------------
STADIUM_COORDINATES: dict[str, tuple[float, float]] = {
    "ARI": (33.445, -112.067),  # Chase Field (retractable)
    "ATL": (33.891,  -84.468),  # Truist Park
    "BAL": (39.284,  -76.622),  # Camden Yards
    "BOS": (42.347,  -71.097),  # Fenway Park
    "CHC": (41.948,  -87.656),  # Wrigley Field
    "CIN": (39.097,  -84.507),  # Great American Ball Park
    "CLE": (41.496,  -81.685),  # Progressive Field
    "COL": (39.756, -104.994),  # Coors Field
    "CWS": (41.830,  -87.634),  # Guaranteed Rate Field
    "DET": (42.339,  -83.049),  # Comerica Park
    "HOU": (29.757,  -95.355),  # Minute Maid Park (retractable)
    "KC":  (39.051,  -94.480),  # Kauffman Stadium
    "LAA": (33.800, -117.883),  # Angel Stadium
    "LAD": (34.074, -118.240),  # Dodger Stadium
    "MIA": (25.778,  -80.220),  # loanDepot Park (retractable)
    "MIL": (43.028,  -88.097),  # American Family Field (retractable)
    "MIN": (44.982,  -93.278),  # Target Field
    "NYM": (40.757,  -73.846),  # Citi Field
    "NYY": (40.829,  -73.926),  # Yankee Stadium
    "ATH": (38.580, -121.500),  # Sutter Health Park, Sacramento
    "PHI": (39.906,  -75.166),  # Citizens Bank Park
    "PIT": (40.447,  -80.006),  # PNC Park
    "SD":  (32.707, -117.157),  # Petco Park
    "SF":  (37.778, -122.389),  # Oracle Park
    "SEA": (47.591, -122.332),  # T-Mobile Park (retractable)
    "STL": (38.623,  -90.193),  # Busch Stadium
    "TB":  (27.768,  -82.654),  # Tropicana Field (indoor)
    "TEX": (32.751,  -97.083),  # Globe Life Field (retractable)
    "TOR": (43.641,  -79.389),  # Rogers Centre (retractable)
    "WSH": (38.873,  -77.008),  # Nationals Park
}

# ---------------------------------------------------------------------------
# Wind-out bearing — the compass degree (0-360, FROM which the wind blows)
# that corresponds to "wind blowing OUT toward center field" at each park.
# A ±45° window is applied in _is_wind_out().
#
# Parks with retractable roofs or indoor environments are omitted — wind is
# irrelevant for them and we never want to award the wind-out bonus.
#
# Degrees follow meteorological convention: 0° = from North, 90° = from East,
# 180° = from South, 270° = from West.
#
# Derivation: each park's home-plate-to-CF bearing determines the direction
# wind must BLOW to carry balls out.  Wind FROM the *opposite* of that bearing
# blows in that direction.  A "from the south" wind blows northward.
# ---------------------------------------------------------------------------
STADIUM_WIND_OUT_FROM_DEG: dict[str, int] = {
    # Outdoor parks only — retractable/indoor are excluded
    "ATL": 310,  # Truist Park: CF faces SE; wind from NW blows out
    "BAL": 315,  # Camden Yards: CF faces SE; wind from NW blows out
    "BOS": 315,  # Fenway: CF faces SE; wind from NW blows out
    "CHC": 220,  # Wrigley: CF faces NE; wind from SW blows out
    "CIN": 230,  # Great American: CF faces NE; wind from SW blows out
    "CLE": 135,  # Progressive: CF faces NW; wind from SE blows out
    "COL": 160,  # Coors: CF faces NNW; wind from SSE blows out
    "CWS": 175,  # Guaranteed Rate: CF faces N; wind from S blows out
    "DET": 160,  # Comerica: CF faces NNW; wind from SSE blows out
    "KC":  170,  # Kauffman: CF faces N; wind from S blows out
    "LAA": 200,  # Angel Stadium: CF faces NNE; wind from SSW blows out
    "LAD": 130,  # Dodger Stadium: CF faces NW; wind from SE blows out
    "MIN": 315,  # Target Field: CF faces SE; wind from NW blows out
    "NYM": 205,  # Citi Field: CF faces NNE; wind from SSW blows out
    "NYY": 175,  # Yankee Stadium: CF faces N; wind from S blows out
    "ATH": 170,  # Sutter Health: outdoor, assuming CF faces N
    "PHI": 205,  # Citizens Bank: CF faces NNE; wind from SSW blows out
    "PIT": 165,  # PNC Park: CF faces NNW; wind from SSE blows out
    "SD":  170,  # Petco Park: CF faces N; wind from S blows out
    "SF":  150,  # Oracle Park: CF faces NW; wind from SE blows out (SF usually blows IN)
    "STL": 315,  # Busch Stadium: CF faces SE; wind from NW blows out
    "WSH": 215,  # Nationals Park: CF faces NNE; wind from SSW blows out
}


def _degrees_to_compass(deg: float) -> str:
    """Convert meteorological degrees to 8-point compass label."""
    idx = int((deg % 360 + 22.5) / 45) % 8
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[idx]


def _is_wind_out(from_deg: float, park_team: str) -> bool:
    """Return True if wind blows out to CF at this park (±45° tolerance)."""
    center = STADIUM_WIND_OUT_FROM_DEG.get(park_team)
    if center is None:
        return False
    diff = abs((from_deg - center + 180) % 360 - 180)
    return diff <= 45


def _classify_wind_direction(from_deg: float, park_team: str | None) -> str:
    """Return 'OUT' if wind blows out to CF, else 8-point compass label."""
    if park_team and _is_wind_out(from_deg, park_team):
        return "OUT"
    return _degrees_to_compass(from_deg)


def _kmh_to_mph(kmh: float) -> float:
    return kmh * 0.621371


def _celsius_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def _extract_hour(data: dict, target_utc_hour: int) -> dict | None:
    """Pick the hourly reading closest to target_utc_hour."""
    hourly    = data.get("hourly", {})
    times     = hourly.get("time", [])
    temps     = hourly.get("temperature_2m", [])
    speeds    = hourly.get("wind_speed_10m", [])
    directions = hourly.get("wind_direction_10m", [])

    if not times:
        return None

    best_idx, best_diff = 0, 999
    for i, t in enumerate(times):
        hour = int(t.split("T")[1][:2])
        diff = abs(hour - target_utc_hour)
        if diff < best_diff:
            best_diff = diff
            best_idx = i

    temp_c    = temps[best_idx]     if best_idx < len(temps)      else None
    speed_kmh = speeds[best_idx]    if best_idx < len(speeds)     else None
    dir_deg   = directions[best_idx] if best_idx < len(directions) else None

    if temp_c is None or speed_kmh is None or dir_deg is None:
        return None

    return {
        "temperature_f":    round(_celsius_to_f(temp_c)),
        "wind_speed_mph":   round(_kmh_to_mph(speed_kmh), 1),
        "wind_direction_deg": round(dir_deg),
    }


async def _fetch(url: str, lat: float, lon: float, game_date: date) -> dict | None:
    params = {
        "latitude":         lat,
        "longitude":        lon,
        "hourly":           "temperature_2m,wind_speed_10m,wind_direction_10m",
        "temperature_unit": "celsius",
        "wind_speed_unit":  "kmh",
        "timezone":         "UTC",
        "start_date":       game_date.isoformat(),
        "end_date":         game_date.isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("Open-Meteo request failed (%s lat=%s lon=%s): %s", url, lat, lon, e)
        return None


async def get_game_weather(
    lat: float,
    lon: float,
    game_date: date,
    game_utc_hour: int,
    park_team: str | None = None,
    use_archive: bool = False,
) -> dict | None:
    """Fetch weather for a game.

    Returns dict with temperature_f (int), wind_speed_mph (float),
    wind_direction (str — "OUT" or 8-point compass), and
    wind_direction_deg (int — raw degrees for observability).

    Returns None on any API failure (weather is non-fatal).
    """
    url = ARCHIVE_URL if use_archive else FORECAST_URL
    data = await _fetch(url, lat, lon, game_date)
    if data is None:
        return None

    extracted = _extract_hour(data, game_utc_hour)
    if extracted is None:
        return None

    extracted["wind_direction"] = _classify_wind_direction(
        extracted["wind_direction_deg"], park_team
    )
    return extracted
