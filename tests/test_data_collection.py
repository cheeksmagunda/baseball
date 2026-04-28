"""
Tests for app/services/data_collection.py

Covers:
- Pure helpers: _safe_float, _format_game_time_et
- enrich_slate_game_vegas_lines: happy path, missing odds raise, API failure
  propagates (no fallback)

All external I/O (MLB API, Odds API) is mocked — no real HTTP calls.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.slate import Slate, SlateGame
from app.services.data_collection import _safe_float, _format_game_time_et


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# _safe_float
# ---------------------------------------------------------------------------

def test_safe_float_numeric_string():
    assert _safe_float("3.14") == pytest.approx(3.14)

def test_safe_float_none_returns_none():
    assert _safe_float(None) is None

def test_safe_float_empty_string_returns_none():
    assert _safe_float("") is None

def test_safe_float_sentinel_dash_returns_none():
    assert _safe_float(".---") is None

def test_safe_float_sentinel_negative_dash_returns_none():
    assert _safe_float("-.--") is None

def test_safe_float_zero():
    assert _safe_float("0.00") == 0.0

def test_safe_float_integer_string():
    assert _safe_float("10") == 10.0


# ---------------------------------------------------------------------------
# _format_game_time_et
# ---------------------------------------------------------------------------

def test_format_game_time_et_strips_leading_zero():
    # "2026-04-11T23:05:00Z" is 7:05 PM ET (UTC-4 in April = EDT)
    result = _format_game_time_et("2026-04-11T23:05:00Z")
    assert result is not None
    assert result.startswith("7:")
    assert "PM ET" in result

def test_format_game_time_et_noon():
    # "2026-04-11T16:10:00Z" is 12:10 PM ET (UTC-4 in April)
    result = _format_game_time_et("2026-04-11T16:10:00Z")
    assert result is not None
    assert "12:10 PM ET" == result

def test_format_game_time_et_none_input_returns_none():
    assert _format_game_time_et(None) is None

def test_format_game_time_et_empty_string_returns_none():
    assert _format_game_time_et("") is None


# ---------------------------------------------------------------------------
# enrich_slate_game_vegas_lines
# ---------------------------------------------------------------------------

def _make_slate_with_game(db, home="NYY", away="BOS", game_status="Preview") -> tuple[Slate, SlateGame]:
    slate = Slate(date=date(2026, 4, 28), game_count=1, status="pending")
    db.add(slate)
    db.flush()
    game = SlateGame(
        slate_id=slate.id,
        home_team=home,
        away_team=away,
        game_status=game_status,
    )
    db.add(game)
    db.flush()
    return slate, game


@pytest.mark.asyncio
async def test_enrich_vegas_lines_happy_path(db):
    """Odds API returns matching lines — should write them onto the SlateGame."""
    from app.services.data_collection import enrich_slate_game_vegas_lines

    slate, game = _make_slate_with_game(db)
    fake_odds = [
        {
            "home_team": "NYY",
            "away_team": "BOS",
            "home_moneyline": -150,
            "away_moneyline": 130,
            "total": 8.5,
        }
    ]

    # fetch_mlb_odds is imported locally inside the function, so patch on the
    # source module (app.core.odds_api) — Python's import cache ensures the
    # local `from app.core.odds_api import fetch_mlb_odds` picks up the mock.
    with patch("app.core.odds_api.fetch_mlb_odds", new_callable=AsyncMock, return_value=fake_odds), \
         patch("app.config.settings") as mock_settings:
        mock_settings.odds_api_key = "test-key"
        updated = await enrich_slate_game_vegas_lines(db, slate)

    assert updated == 1
    db.refresh(game)
    assert game.home_moneyline == -150
    assert game.away_moneyline == 130
    assert game.vegas_total == 8.5


@pytest.mark.asyncio
async def test_enrich_vegas_lines_no_match_raises(db):
    """If Odds API returns lines for a different team pairing, raise — no fallback."""
    from app.services.data_collection import enrich_slate_game_vegas_lines

    slate, _ = _make_slate_with_game(db, home="NYY", away="BOS")
    fake_odds = [
        {
            "home_team": "LAD",
            "away_team": "SF",
            "home_moneyline": -180,
            "away_moneyline": 160,
            "total": 9.0,
        }
    ]

    with patch("app.core.odds_api.fetch_mlb_odds", new_callable=AsyncMock, return_value=fake_odds), \
         patch("app.config.settings") as mock_settings:
        mock_settings.odds_api_key = "test-key"
        with pytest.raises(RuntimeError, match="No odds found for NYY vs BOS"):
            await enrich_slate_game_vegas_lines(db, slate)


@pytest.mark.asyncio
async def test_enrich_vegas_lines_api_failure_propagates(db):
    """Odds API RuntimeError must bubble up — never swallowed."""
    from app.services.data_collection import enrich_slate_game_vegas_lines

    slate, _ = _make_slate_with_game(db)

    with patch(
        "app.core.odds_api.fetch_mlb_odds",
        new_callable=AsyncMock,
        side_effect=RuntimeError("quota exhausted"),
    ), patch("app.config.settings") as mock_settings:
        mock_settings.odds_api_key = "test-key"
        with pytest.raises(RuntimeError, match="quota exhausted"):
            await enrich_slate_game_vegas_lines(db, slate)


@pytest.mark.asyncio
async def test_enrich_vegas_lines_skips_started_games(db):
    """Games with status 'Live' or 'Final' must be filtered out; no API call if none remain."""
    from app.services.data_collection import enrich_slate_game_vegas_lines

    slate = Slate(date=date(2026, 4, 28), game_count=1, status="active")
    db.add(slate)
    db.flush()
    game = SlateGame(
        slate_id=slate.id,
        home_team="NYY",
        away_team="BOS",
        game_status="Final",
    )
    db.add(game)
    db.flush()

    with patch("app.core.odds_api.fetch_mlb_odds", new_callable=AsyncMock) as mock_fetch:
        result = await enrich_slate_game_vegas_lines(db, slate)

    assert result == 0
    mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_enrich_vegas_lines_skips_if_already_populated(db):
    """If all games already have moneylines, skip the API call."""
    from app.services.data_collection import enrich_slate_game_vegas_lines

    slate = Slate(date=date(2026, 4, 28), game_count=1, status="pending")
    db.add(slate)
    db.flush()
    game = SlateGame(
        slate_id=slate.id,
        home_team="NYY",
        away_team="BOS",
        game_status="Preview",
        home_moneyline=-150,
        away_moneyline=130,
    )
    db.add(game)
    db.flush()

    with patch("app.core.odds_api.fetch_mlb_odds", new_callable=AsyncMock) as mock_fetch:
        result = await enrich_slate_game_vegas_lines(db, slate)

    assert result == 1
    mock_fetch.assert_not_called()
