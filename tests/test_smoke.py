"""
Smoke tests covering the pipeline, lineup_cache, slate_monitor, and the
filter_strategy router.

These are pinholes — enough to catch import regressions, signature drift,
and the key invariants (Redis meta persistence, T-65 gating, batched
starter-stats cache). They are intentionally thin; the full filter-strategy
logic is covered by test_filter_strategy.py.

Isolation guarantees (defense in depth):
  * Every test uses an in-memory SQLite DB (StaticPool) — the real
    db/ben_oracle.db file is never opened or written.
  * Every test uses a fresh _LineupCache() instance with Redis pre-mocked —
    no real Redis connection is ever attempted.
  * The module-level lineup_cache singleton is asserted to be untouched
    after every test (see _global_lineup_cache_untouched autouse fixture).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.player import Player, PlayerStats, normalize_name
from app.models.slate import Slate, SlateGame


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _global_lineup_cache_untouched():
    """Fail any test that leaks state into the module-level lineup_cache.

    Smoke tests always use a fresh _LineupCache() via the fresh_cache fixture.
    If a future test ever mutates app.services.lineup_cache.lineup_cache
    directly, this guard fails loudly so the bleed is caught immediately.
    """
    from app.services.lineup_cache import lineup_cache
    snapshot = (
        lineup_cache._data,
        lineup_cache._slate_date,
        lineup_cache._is_frozen,
        lineup_cache._first_pitch_utc,
    )
    yield
    assert (
        lineup_cache._data,
        lineup_cache._slate_date,
        lineup_cache._is_frozen,
        lineup_cache._first_pitch_utc,
    ) == snapshot, (
        "Test mutated the module-level lineup_cache singleton. "
        "Use the fresh_cache fixture instead."
    )


@pytest.fixture
def db_session():
    """In-memory SQLite session with all tables created.

    StaticPool shares a single connection across threads so FastAPI's
    TestClient (which dispatches async handlers on a worker thread) sees
    the same schema the fixture set up. The engine is disposed at teardown,
    which drops the in-memory database — no disk footprint.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def fresh_cache(monkeypatch):
    """Isolated _LineupCache with Redis pre-mocked and router rebinding applied.

    The fixture:
      1. Creates a brand-new _LineupCache() — never touches the module-level
         singleton.
      2. Pre-fills ._redis with a MagicMock and sets ._redis_checked=True so
         _get_redis() returns the mock without ever reading settings or
         opening a socket.
      3. monkeypatch-rebinds app.routers.filter_strategy.lineup_cache to this
         fresh instance so router tests see it without boilerplate — the
         rebinding is reverted at teardown.
    """
    from app.services import lineup_cache as lc_module
    from app.routers import filter_strategy as router_module

    cache = lc_module._LineupCache()
    redis = MagicMock()
    redis.get.return_value = None
    cache._redis = redis
    cache._redis_checked = True

    monkeypatch.setattr(router_module, "lineup_cache", cache)
    return cache, redis


# ---------------------------------------------------------------------------
# pipeline._build_starter_stats_cache
# ---------------------------------------------------------------------------

class TestBuildStarterStatsCache:
    def test_empty_games_returns_empty_dict(self, db_session):
        from app.services.pipeline import _build_starter_stats_cache
        assert _build_starter_stats_cache(db_session, [], 2026) == {}

    def test_games_without_starters_return_empty(self, db_session):
        from app.services.pipeline import _build_starter_stats_cache
        slate = Slate(date=date(2026, 4, 17))
        db_session.add(slate)
        db_session.flush()
        g = SlateGame(slate_id=slate.id, home_team="NYY", away_team="BOS")
        db_session.add(g)
        db_session.flush()
        assert _build_starter_stats_cache(db_session, [g], 2026) == {}

    def test_batches_stats_for_named_starters(self, db_session):
        from app.services.pipeline import _build_starter_stats_cache

        gerrit = Player(
            name="Gerrit Cole", name_normalized=normalize_name("Gerrit Cole"),
            team="NYY", position="P", pitch_hand="R",
        )
        chris = Player(
            name="Chris Sale", name_normalized=normalize_name("Chris Sale"),
            team="BOS", position="P", pitch_hand="L",
        )
        db_session.add_all([gerrit, chris])
        db_session.flush()
        db_session.add_all([
            PlayerStats(player_id=gerrit.id, season=2026, era=3.10, whip=1.05, k_per_9=10.5),
            PlayerStats(player_id=chris.id, season=2026, era=4.20, whip=1.25, k_per_9=9.0),
        ])

        slate = Slate(date=date(2026, 4, 17))
        db_session.add(slate)
        db_session.flush()
        game = SlateGame(
            slate_id=slate.id, home_team="NYY", away_team="BOS",
            home_starter="Gerrit Cole", away_starter="Chris Sale",
        )
        db_session.add(game)
        db_session.flush()

        cache = _build_starter_stats_cache(db_session, [game], 2026)
        assert cache["Gerrit Cole"]["era"] == 3.10
        assert cache["Gerrit Cole"]["pitch_hand"] == "R"
        assert cache["Chris Sale"]["k_per_9"] == 9.0
        assert cache["Chris Sale"]["pitch_hand"] == "L"

    def test_missing_starter_maps_to_empty_dict(self, db_session):
        from app.services.pipeline import _build_starter_stats_cache
        slate = Slate(date=date(2026, 4, 17))
        db_session.add(slate)
        db_session.flush()
        game = SlateGame(
            slate_id=slate.id, home_team="NYY", away_team="BOS",
            home_starter="Phantom Pitcher",
        )
        db_session.add(game)
        db_session.flush()

        cache = _build_starter_stats_cache(db_session, [game], 2026)
        assert cache["Phantom Pitcher"] == {}


# ---------------------------------------------------------------------------
# lineup_cache
# ---------------------------------------------------------------------------

class TestLineupCache:
    def test_initial_state(self, fresh_cache):
        cache, _ = fresh_cache
        assert cache.is_frozen is False
        assert cache.is_warm is False
        assert cache.first_pitch_utc is None
        assert cache.lock_time_utc is None
        assert cache.unlock_time_utc is None

    def test_set_schedule_computes_lock_and_unlock(self, fresh_cache):
        cache, _ = fresh_cache
        first_pitch = datetime(2026, 4, 17, 23, 5, tzinfo=timezone.utc)
        cache.set_schedule(first_pitch)

        assert cache.first_pitch_utc == first_pitch
        assert cache.lock_time_utc == first_pitch - timedelta(minutes=65)
        assert cache.unlock_time_utc == first_pitch - timedelta(minutes=60)

    def test_freeze_sets_frozen_and_persists_meta(self, fresh_cache):
        cache, redis = fresh_cache
        first_pitch = datetime(2026, 4, 17, 23, 5, tzinfo=timezone.utc)
        cache.freeze(first_pitch)

        assert cache.is_frozen is True
        assert cache.first_pitch_utc == first_pitch
        # meta persisted to Redis so a restart re-inherits the locked state.
        assert redis.setex.called
        meta_calls = [c for c in redis.setex.call_args_list if "meta" in c.args[0]]
        assert meta_calls, "freeze() must write meta key to Redis"

    def test_store_no_ops_when_frozen(self, fresh_cache):
        cache, redis = fresh_cache
        cache.freeze(datetime(2026, 4, 17, 23, 5, tzinfo=timezone.utc))
        redis.setex.reset_mock()

        response = MagicMock()
        response.model_dump_json.return_value = "{}"
        cache.store(response, date(2026, 4, 17))
        # No payload write — store() is a no-op while frozen.
        payload_calls = [c for c in redis.setex.call_args_list
                         if c.args[0].startswith("lineup:") and not c.args[0].startswith("lineup:meta")]
        assert payload_calls == []

    def test_clear_resets_everything(self, fresh_cache):
        cache, _ = fresh_cache
        cache.freeze(datetime(2026, 4, 17, 23, 5, tzinfo=timezone.utc))
        cache.clear()
        assert cache.is_frozen is False
        assert cache.first_pitch_utc is None
        assert cache._data is None


# ---------------------------------------------------------------------------
# slate_monitor pure helpers
# ---------------------------------------------------------------------------

class TestParseGameTime:
    def test_evening_et_in_edt_window(self):
        from app.services.slate_monitor import _parse_game_time
        # April 17 is EDT (UTC-4). 7:05 PM ET = 23:05 UTC.
        result = _parse_game_time("7:05 PM ET", date(2026, 4, 17))
        assert result == datetime(2026, 4, 17, 23, 5, tzinfo=timezone.utc)

    def test_afternoon_et(self):
        from app.services.slate_monitor import _parse_game_time
        result = _parse_game_time("1:10 PM ET", date(2026, 4, 17))
        assert result == datetime(2026, 4, 17, 17, 10, tzinfo=timezone.utc)

    def test_late_pt_rolls_to_next_day(self):
        from app.services.slate_monitor import _parse_game_time
        # 10:10 PM PT on 4/17 → 1:10 AM ET on 4/18 → 05:10 UTC on 4/18.
        # The suffix is stripped; the clock is interpreted as ET.
        # The <5 AM guard rolls the date forward so the UTC result lands
        # on the following calendar day — preventing a T-65 midnight lock.
        result = _parse_game_time("1:10 AM PT", date(2026, 4, 17))
        assert result == datetime(2026, 4, 18, 5, 10, tzinfo=timezone.utc)

    def test_unparseable_returns_none(self):
        from app.services.slate_monitor import _parse_game_time
        assert _parse_game_time("not a time", date(2026, 4, 17)) is None

    def test_empty_returns_none(self):
        from app.services.slate_monitor import _parse_game_time
        assert _parse_game_time("", date(2026, 4, 17)) is None


class TestGetFirstPitchUtc:
    def test_returns_none_when_no_slate(self, db_session):
        from app.services.slate_monitor import _get_first_pitch_utc
        assert _get_first_pitch_utc(db_session, date(2026, 4, 17)) is None

    def test_returns_earliest_parsed_time(self, db_session):
        from app.services.slate_monitor import _get_first_pitch_utc

        slate = Slate(date=date(2026, 4, 17))
        db_session.add(slate)
        db_session.flush()
        db_session.add_all([
            SlateGame(slate_id=slate.id, home_team="NYY", away_team="BOS",
                      scheduled_game_time="7:05 PM ET"),
            SlateGame(slate_id=slate.id, home_team="LAD", away_team="SF",
                      scheduled_game_time="1:10 PM ET"),
            SlateGame(slate_id=slate.id, home_team="CHC", away_team="STL",
                      scheduled_game_time="4:05 PM ET"),
        ])
        db_session.flush()

        earliest = _get_first_pitch_utc(db_session, date(2026, 4, 17))
        assert earliest == datetime(2026, 4, 17, 17, 10, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Router integration — /api/filter-strategy/{status,optimize}
# ---------------------------------------------------------------------------

def _build_test_app():
    """Build a minimal FastAPI app wired to the filter-strategy router."""
    from app.routers import filter_strategy as router_module
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/filter-strategy")
    return app


class TestIsolation:
    """Proof-of-isolation tests: verify fake data cannot bleed into real systems."""

    def test_fresh_cache_does_not_touch_real_redis(self, fresh_cache):
        """Freezing a fresh cache must only write to the mock, never a real client."""
        cache, redis = fresh_cache
        cache.freeze(datetime(2026, 4, 17, 23, 5, tzinfo=timezone.utc))
        # All Redis traffic lands on the mock; the cache never re-entered _get_redis.
        assert redis.setex.called
        # The settings-driven branch of _get_redis is bypassed because _redis is pre-filled.
        assert cache._redis is redis

    def test_db_session_is_in_memory_not_real_file(self, db_session):
        """The fixture engine must be a sqlite:// (in-memory) URL — never the real DB."""
        url = str(db_session.get_bind().url)
        assert url.startswith("sqlite://") and ":memory:" not in url  # sqlite:// == anonymous in-memory
        # Real app DB lives on disk; fixture never touches it.
        from app.config import settings
        assert settings.database_url != url


class TestFilterStrategyRouter:
    def test_status_no_slate(self, fresh_cache):
        client = TestClient(_build_test_app())
        response = client.get("/api/filter-strategy/status")
        assert response.status_code == 200
        body = response.json()
        assert body["phase"] == "no_slate"
        assert body["ready"] is False

    def test_status_before_lock(self, fresh_cache):
        cache, _ = fresh_cache
        far_future = datetime.now(timezone.utc) + timedelta(hours=24)
        cache.set_schedule(far_future)
        client = TestClient(_build_test_app())
        response = client.get("/api/filter-strategy/status")
        body = response.json()
        assert body["phase"] == "before_lock"
        assert body["ready"] is False
        assert body["minutes_until_unlock"] > 0

    def test_optimize_returns_425_before_lock(self, db_session, fresh_cache):
        cache, _ = fresh_cache
        far_future = datetime.now(timezone.utc) + timedelta(hours=24)
        cache.set_schedule(far_future)

        from app.database import get_db

        app = _build_test_app()
        app.dependency_overrides[get_db] = lambda: db_session
        client = TestClient(app)
        response = client.get("/api/filter-strategy/optimize")

        assert response.status_code == 425
        body = response.json()
        assert "Pipeline runs at T-65" in body["detail"]
