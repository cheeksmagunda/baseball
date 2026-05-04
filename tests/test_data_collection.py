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


# ---------------------------------------------------------------------------
# resolve_mlb_id — must raise on lookup failure (May 2026 strict pass).
# Returning None silently was masking the real upstream bug; downstream
# callers either crashed later (cascading None) or silently dropped the
# player from the candidate pool — both worse than failing fast at the
# source.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_mlb_id_returns_existing_id_without_api_call(db):
    from app.models.player import Player
    from app.services.data_collection import resolve_mlb_id

    player = Player(name="Aaron Judge", name_normalized="aaron judge",
                    team="NYY", position="OF", mlb_id=592450)
    db.add(player)
    db.flush()

    with patch("app.services.data_collection.search_player", new_callable=AsyncMock) as mock_search:
        result = await resolve_mlb_id(db, player)

    assert result == 592450
    mock_search.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_mlb_id_matches_team_in_search_results(db):
    from app.models.player import Player
    from app.services.data_collection import resolve_mlb_id

    player = Player(name="Aaron Judge", name_normalized="aaron judge",
                    team="NYY", position="OF")
    db.add(player)
    db.flush()

    fake_results = [
        {"id": 99999, "currentTeam": {"abbreviation": "BOS"}},
        {"id": 592450, "currentTeam": {"abbreviation": "NYY"}},
    ]
    with patch("app.services.data_collection.search_player",
               new_callable=AsyncMock, return_value=fake_results):
        result = await resolve_mlb_id(db, player)

    assert result == 592450
    db.refresh(player)
    assert player.mlb_id == 592450


@pytest.mark.asyncio
async def test_resolve_mlb_id_raises_when_no_search_results(db):
    """Empty /people/search must crash, not silently return None."""
    from app.models.player import Player
    from app.services.data_collection import resolve_mlb_id

    player = Player(name="Ghost Player", name_normalized="ghost player",
                    team="NYY", position="OF")
    db.add(player)
    db.flush()

    with patch("app.services.data_collection.search_player",
               new_callable=AsyncMock, return_value=[]):
        with pytest.raises(RuntimeError, match="returned 0 results"):
            await resolve_mlb_id(db, player)


@pytest.mark.asyncio
async def test_resolve_mlb_id_raises_when_no_team_match(db):
    """Search results without a team match must crash — refusing to guess
    avoids assigning the wrong player's stats to this row."""
    from app.models.player import Player
    from app.services.data_collection import resolve_mlb_id

    player = Player(name="John Smith", name_normalized="john smith",
                    team="NYY", position="OF")
    db.add(player)
    db.flush()

    # Two candidates, neither on NYY — refuse to guess.
    fake_results = [
        {"id": 1, "currentTeam": {"abbreviation": "BOS"}},
        {"id": 2, "currentTeam": {"abbreviation": "LAD"}},
    ]
    with patch("app.services.data_collection.search_player",
               new_callable=AsyncMock, return_value=fake_results):
        with pytest.raises(RuntimeError, match="none matched team 'NYY'"):
            await resolve_mlb_id(db, player)


# ---------------------------------------------------------------------------
# Distributed tracing — every outbound httpx request must carry the active
# correlation ID as the X-Request-ID header.  The hook is wired into the
# four module-level AsyncClients (mlb_api, odds_api, open_meteo, rotowire)
# via event_hooks={"request": [tracing_event_hook]}.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tracing_hook_attaches_header_when_run_id_set():
    import httpx
    from app.core.logging_config import (
        request_id_var,
        set_pipeline_run_id,
        tracing_event_hook,
    )

    rid, token = set_pipeline_run_id()
    try:
        req = httpx.Request("GET", "http://example.com/")
        await tracing_event_hook(req)
        assert req.headers["X-Request-ID"] == rid
    finally:
        request_id_var.reset(token)


@pytest.mark.asyncio
async def test_tracing_hook_skips_header_when_default():
    """Default request_id_var is "-" (no run set).  Don't pollute outbound
    requests with a placeholder."""
    import httpx
    from app.core.logging_config import tracing_event_hook

    req = httpx.Request("GET", "http://example.com/")
    await tracing_event_hook(req)
    assert "X-Request-ID" not in req.headers


def test_all_http_clients_have_tracing_hook_wired():
    """Every module-level AsyncClient in app/core must include the tracing
    hook in its request event hooks — distributed tracing relies on this."""
    from app.core import mlb_api, odds_api, open_meteo, rotowire
    from app.core.logging_config import tracing_event_hook

    for module, name in (
        (mlb_api, "mlb_api"),
        (odds_api, "odds_api"),
        (open_meteo, "open_meteo"),
        (rotowire, "rotowire"),
    ):
        hooks = module._CLIENT.event_hooks.get("request", [])
        assert tracing_event_hook in hooks, (
            f"{name}._CLIENT is missing tracing_event_hook — distributed "
            "tracing will not propagate the correlation ID downstream."
        )
