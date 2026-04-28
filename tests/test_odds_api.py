"""
Tests for app/core/odds_api.py — The Odds API client.

Vegas lines are mandatory for the pipeline; any failure must raise loudly.
These tests mock the HTTP layer so no real API key or network is needed.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.odds_api import _match_team, fetch_mlb_odds


# ---------------------------------------------------------------------------
# _match_team — team-name fragment matching
# ---------------------------------------------------------------------------

def test_match_team_yankees():
    assert _match_team("New York Yankees") == "NYY"

def test_match_team_dodgers():
    assert _match_team("Los Angeles Dodgers") == "LAD"

def test_match_team_red_sox():
    assert _match_team("Boston Red Sox") == "BOS"

def test_match_team_cubs():
    assert _match_team("Chicago Cubs") == "CHC"

def test_match_team_white_sox():
    assert _match_team("Chicago White Sox") == "CWS"

def test_match_team_athletics():
    assert _match_team("Athletics") == "ATH"

def test_match_team_unknown_returns_none():
    assert _match_team("Unknown Franchise") is None

def test_match_team_case_insensitive():
    assert _match_team("new york yankees") == "NYY"


# ---------------------------------------------------------------------------
# fetch_mlb_odds — error handling (no real HTTP calls)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_mlb_odds_empty_key_raises():
    with pytest.raises(RuntimeError, match="BO_ODDS_API_KEY"):
        await fetch_mlb_odds("", date(2026, 4, 28))


@pytest.mark.asyncio
async def test_fetch_mlb_odds_none_key_raises():
    with pytest.raises(RuntimeError, match="BO_ODDS_API_KEY"):
        await fetch_mlb_odds(None, date(2026, 4, 28))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_mlb_odds_401_raises():
    mock_response = MagicMock()
    mock_response.status_code = 401

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
        with pytest.raises(RuntimeError, match="invalid API key"):
            await fetch_mlb_odds("valid_key", date(2026, 4, 28))


@pytest.mark.asyncio
async def test_fetch_mlb_odds_422_raises():
    mock_response = MagicMock()
    mock_response.status_code = 422

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
        with pytest.raises(RuntimeError, match="quota exhausted"):
            await fetch_mlb_odds("valid_key", date(2026, 4, 28))


@pytest.mark.asyncio
async def test_fetch_mlb_odds_500_raises():
    mock_response = MagicMock()
    mock_response.status_code = 500

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
        with pytest.raises(RuntimeError, match="unexpected status 500"):
            await fetch_mlb_odds("valid_key", date(2026, 4, 28))


@pytest.mark.asyncio
async def test_fetch_mlb_odds_success_parses_response():
    """Happy path: response with one NYY vs BOS game."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"x-requests-remaining": "490"}
    mock_response.json.return_value = [
        {
            "id": "abc",
            "home_team": "New York Yankees",
            "away_team": "Boston Red Sox",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "New York Yankees", "price": -150},
                                {"name": "Boston Red Sox", "price": 130},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "point": 8.5},
                                {"name": "Under", "point": 8.5},
                            ],
                        },
                    ],
                }
            ],
        }
    ]

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
        result = await fetch_mlb_odds("valid_key", date(2026, 4, 28))

    assert len(result) == 1
    game = result[0]
    assert game["home_team"] == "NYY"
    assert game["away_team"] == "BOS"
    assert game["home_moneyline"] == -150
    assert game["away_moneyline"] == 130
    assert game["total"] == 8.5


@pytest.mark.asyncio
async def test_fetch_mlb_odds_unknown_team_raises():
    """Unrecognised team name must raise rather than silently emit None."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"x-requests-remaining": "490"}
    mock_response.json.return_value = [
        {
            "id": "xyz",
            "home_team": "Fictional City Robots",
            "away_team": "Boston Red Sox",
            "bookmakers": [],
        }
    ]

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
        with pytest.raises(RuntimeError, match="could not match teams"):
            await fetch_mlb_odds("valid_key", date(2026, 4, 28))


@pytest.mark.asyncio
async def test_fetch_mlb_odds_empty_events():
    """Odds API returning zero events (off-day) should return an empty list."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"x-requests-remaining": "490"}
    mock_response.json.return_value = []

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
        result = await fetch_mlb_odds("valid_key", date(2026, 4, 28))

    assert result == []
