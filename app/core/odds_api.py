"""
The Odds API client — fetches pre-game Vegas lines for MLB games.

Used to populate SlateGame.vegas_total, home_moneyline, away_moneyline.
Free tier: 500 requests/month.  The pipeline fails loudly if the key is
missing or the API returns an error — no fallback to neutral defaults.

API endpoint: GET /v4/sports/baseball_mlb/odds
Docs: https://the-odds-api.com/laps-api.html
"""

import logging
from datetime import date, timedelta

import httpx
import tenacity

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.the-odds-api.com"
_TIMEOUT = 15.0

# Maps MLB team abbreviations to the team-name fragments used in The Odds API.
# The Odds API uses full city/franchise names; we match on these fragments.
ODDS_API_TEAM_FRAGMENTS: dict[str, list[str]] = {
    "ARI": ["Arizona", "Diamondbacks"],
    "ATL": ["Atlanta", "Braves"],
    "BAL": ["Baltimore", "Orioles"],
    "BOS": ["Boston", "Red Sox"],
    "CHC": ["Chicago Cubs", "Cubs"],
    "CWS": ["Chicago White Sox", "White Sox"],
    "CIN": ["Cincinnati", "Reds"],
    "CLE": ["Cleveland", "Guardians"],
    "COL": ["Colorado", "Rockies"],
    "DET": ["Detroit", "Tigers"],
    "HOU": ["Houston", "Astros"],
    "KC":  ["Kansas City", "Royals"],
    "LAA": ["Los Angeles Angels", "Angels"],
    "LAD": ["Los Angeles Dodgers", "Dodgers"],
    "MIA": ["Miami", "Marlins"],
    "MIL": ["Milwaukee", "Brewers"],
    "MIN": ["Minnesota", "Twins"],
    "NYM": ["New York Mets", "Mets"],
    "NYY": ["New York Yankees", "Yankees"],
    "ATH": ["Athletics", "Oakland"],
    "PHI": ["Philadelphia", "Phillies"],
    "PIT": ["Pittsburgh", "Pirates"],
    "SD":  ["San Diego", "Padres"],
    "SF":  ["San Francisco", "Giants"],
    "SEA": ["Seattle", "Mariners"],
    "STL": ["St. Louis", "Cardinals"],
    "TB":  ["Tampa Bay", "Rays"],
    "TEX": ["Texas", "Rangers"],
    "TOR": ["Toronto", "Blue Jays"],
    "WSH": ["Washington", "Nationals"],
}


def _match_team(api_name: str) -> str | None:
    """Match an Odds API team name to an MLB abbreviation."""
    for abbr, fragments in ODDS_API_TEAM_FRAGMENTS.items():
        if any(frag.lower() in api_name.lower() for frag in fragments):
            return abbr
    return None


async def fetch_mlb_odds(api_key: str, game_date: date) -> list[dict]:
    """
    Fetch MLB moneylines and totals from The Odds API for a given date.

    Returns a list of dicts:
        [{
            "home_team": "NYY",   # MLB abbreviation
            "away_team": "BOS",
            "home_moneyline": -150,
            "away_moneyline": 130,
            "total": 8.5,         # over/under (None if unavailable)
        }, ...]

    Raises RuntimeError if the API key is missing, quota is exhausted, or
    the request fails — no fallback behavior per "no fallbacks ever" rule.
    """
    if not api_key:
        raise RuntimeError(
            "CRITICAL: BO_ODDS_API_KEY environment variable must be set. "
            "Vegas lines (moneyline + O/U totals) are required inputs to pitcher and batter "
            "environmental scoring. The system cannot optimize lineups without Vegas data. "
            "Set BO_ODDS_API_KEY to your The Odds API key (free tier: 500 requests/month)."
        )

    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "h2h,totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
        "commenceTimeFrom": f"{game_date.isoformat()}T00:00:00Z",
        # Extend to 08:00 UTC next day to capture late Pacific games
        # (e.g., 10:05 PM PDT = 01:05 AM UTC). Safe upper bound: no MLB
        # game starts before noon ET (~16:00 UTC) the following day.
        "commenceTimeTo": f"{(game_date + timedelta(days=1)).isoformat()}T08:00:00Z",
    }

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _fetch() -> httpx.Response:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            return await client.get(f"{_BASE_URL}/v4/sports/baseball_mlb/odds", params=params)

    resp = await _fetch()

    if resp.status_code == 401:
        raise RuntimeError("The Odds API: invalid API key (401)")
    if resp.status_code == 422:
        raise RuntimeError("The Odds API: quota exhausted (422)")
    if resp.status_code != 200:
        raise RuntimeError(f"The Odds API: unexpected status {resp.status_code}")

    remaining = resp.headers.get("x-requests-remaining", "?")
    logger.info("The Odds API: %s requests remaining this month", remaining)

    events = resp.json()
    result = []

    for event in events:
        home_name = event.get("home_team", "")
        away_name = event.get("away_team", "")

        home_abbr = _match_team(home_name)
        away_abbr = _match_team(away_name)
        if not home_abbr or not away_abbr:
            raise RuntimeError(
                f"Odds API: could not match teams {home_name!r} vs {away_name!r} to MLB "
                "abbreviations. Update ODDS_API_TEAM_FRAGMENTS in app/core/odds_api.py."
            )

        home_ml: int | None = None
        away_ml: int | None = None
        total: float | None = None

        for bookmaker in event.get("bookmakers", [])[:1]:  # use first bookmaker
            for market in bookmaker.get("markets", []):
                if market["key"] == "h2h":
                    for outcome in market.get("outcomes", []):
                        t = _match_team(outcome.get("name", ""))
                        price = outcome.get("price")
                        if price is None:
                            continue
                        if t == home_abbr:
                            home_ml = int(price)
                        elif t == away_abbr:
                            away_ml = int(price)
                elif market["key"] == "totals":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name", "").lower() == "over":
                            total = outcome.get("point")

        result.append({
            "home_team": home_abbr,
            "away_team": away_abbr,
            "home_moneyline": home_ml,
            "away_moneyline": away_ml,
            "total": total,
        })

    return result
