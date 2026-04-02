"""MLB Stats API client wrapper."""

import httpx

from app.config import settings

TIMEOUT = 15.0


async def _get(path: str, params: dict | None = None) -> dict:
    """Make a GET request to the MLB Stats API."""
    url = f"{settings.mlb_api_base_url}{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


async def get_schedule(game_date: str) -> dict:
    """Get games for a date. Format: YYYY-MM-DD."""
    return await _get("/schedule", {
        "date": game_date,
        "sportId": 1,
        "hydrate": "probablePitcher,team",
    })


async def get_player_stats(mlb_id: int, season: int) -> dict:
    """Get season stats for a player."""
    return await _get(f"/people/{mlb_id}", {
        "hydrate": f"stats(group=[hitting,pitching],type=[season,gameLog],season={season})",
    })


async def get_game_boxscore(game_pk: int) -> dict:
    """Get box score for a specific game."""
    return await _get(f"/game/{game_pk}/boxscore")


async def get_team_roster(team_id: int) -> dict:
    """Get active roster."""
    return await _get(f"/teams/{team_id}/roster", {"rosterType": "active"})


async def get_team_stats(team_id: int, season: int) -> dict:
    """Get team aggregate stats."""
    return await _get(f"/teams/{team_id}/stats", {
        "stats": "season",
        "group": "hitting",
        "season": season,
    })


async def search_player(name: str) -> list[dict]:
    """Search for a player by name."""
    data = await _get("/people/search", {"names": name, "sportId": 1})
    return data.get("people", [])


# MLB team ID mapping (2026 — these are stable across seasons)
TEAM_MLB_IDS = {
    "ARI": 109, "ATL": 144, "BAL": 110, "BOS": 111, "CHC": 112,
    "CWS": 145, "CIN": 113, "CLE": 114, "COL": 115, "DET": 116,
    "HOU": 117, "KC": 118, "LAA": 108, "LAD": 119, "MIA": 146,
    "MIL": 158, "MIN": 142, "NYM": 121, "NYY": 147, "OAK": 133,
    "PHI": 143, "PIT": 134, "SD": 135, "SF": 137, "SEA": 136,
    "STL": 138, "TB": 139, "TEX": 140, "TOR": 141, "WSH": 120,
}
