"""
Tests for app/services/lineup_cache._LineupCache

All tests use a fresh _LineupCache() instance so the module-level singleton
is never mutated (the autouse guard in test_smoke.py enforces this globally).

Redis is mocked at the class level — no real Redis connection is attempted.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services.lineup_cache import _LineupCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cache(redis_mock: MagicMock | None = None) -> _LineupCache:
    cache = _LineupCache()
    if redis_mock is None:
        redis_mock = MagicMock()
        redis_mock.ping.return_value = True
        redis_mock.get.return_value = None
        redis_mock.setex.return_value = True
        redis_mock.delete.return_value = 1
    cache._redis = redis_mock
    cache._redis_checked = True
    return cache


def _fake_response(json_str: str = '{"starting_5":{},"moonshot":{}}'):
    """Return a minimal mock that satisfies model_dump_json()."""
    mock = MagicMock()
    mock.model_dump_json.return_value = json_str
    return mock


# ---------------------------------------------------------------------------
# Basic state transitions
# ---------------------------------------------------------------------------

def test_cache_starts_empty():
    cache = _make_cache()
    assert cache.get() is None
    assert not cache.is_warm
    assert not cache.is_frozen


def test_store_sets_data():
    cache = _make_cache()
    resp = _fake_response()
    with patch.object(cache, "_persist"):
        cache.store(resp, slate_date=date(2026, 4, 28))
    assert cache._data is resp
    assert cache._slate_date == date(2026, 4, 28)
    assert cache.is_warm


def test_get_returns_data_same_day(monkeypatch):
    cache = _make_cache()
    resp = _fake_response()
    target_date = date(2026, 4, 28)
    with patch.object(cache, "_persist"):
        cache.store(resp, slate_date=target_date)
    monkeypatch.setattr("app.services.lineup_cache.date", type("_D", (), {"today": staticmethod(lambda: target_date)})())
    assert cache.get() is resp


def test_store_calls_redis_setex():
    redis_mock = MagicMock()
    redis_mock.ping.return_value = True
    redis_mock.get.return_value = None
    redis_mock.setex.return_value = True
    redis_mock.delete.return_value = 1
    cache = _make_cache(redis_mock)

    with patch.object(cache, "_persist"):  # skip SQLite write
        cache.store(_fake_response(), slate_date=date(2026, 4, 28))

    redis_mock.setex.assert_called_once()
    call_args = redis_mock.setex.call_args[0]
    assert "2026-04-28" in call_args[0]


# ---------------------------------------------------------------------------
# Freeze invariants
# ---------------------------------------------------------------------------

def test_freeze_blocks_subsequent_store():
    cache = _make_cache()
    fp = datetime(2026, 4, 28, 23, 5, tzinfo=timezone.utc)
    cache.freeze(first_pitch_utc=fp)
    assert cache.is_frozen

    resp_before = _fake_response()
    resp_after = _fake_response()
    # Prime with something first so we can tell it wasn't overwritten
    cache._data = resp_before

    cache.store(resp_after, slate_date=date(2026, 4, 28))
    assert cache._data is resp_before  # unchanged — freeze blocked the write


def test_freeze_sets_first_pitch():
    cache = _make_cache()
    fp = datetime(2026, 4, 28, 23, 5, tzinfo=timezone.utc)
    cache.freeze(first_pitch_utc=fp)
    assert cache.first_pitch_utc == fp


def test_lock_time_is_65_minutes_before():
    cache = _make_cache()
    from datetime import timedelta
    fp = datetime(2026, 4, 28, 23, 5, tzinfo=timezone.utc)
    cache._first_pitch_utc = fp
    expected_lock = fp - timedelta(minutes=65)
    assert cache.lock_time_utc == expected_lock


# ---------------------------------------------------------------------------
# Redis failure raises — no silent degradation
# ---------------------------------------------------------------------------

def test_get_redis_raises_when_no_url():
    cache = _LineupCache()  # unpatched — _redis is None
    with patch("app.config.settings") as mock_settings:
        mock_settings.redis_url = None
        with pytest.raises(RuntimeError, match="BO_REDIS_URL"):
            cache._get_redis()


def test_store_raises_on_redis_failure():
    redis_mock = MagicMock()
    redis_mock.ping.return_value = True
    redis_mock.setex.side_effect = ConnectionError("Redis connection refused")
    cache = _make_cache(redis_mock)

    with pytest.raises(ConnectionError):
        with patch.object(cache, "_persist"):
            cache.store(_fake_response(), slate_date=date(2026, 4, 28))


# ---------------------------------------------------------------------------
# clear() resets all state
# ---------------------------------------------------------------------------

def test_clear_resets_state():
    cache = _make_cache()
    fp = datetime(2026, 4, 28, 23, 5, tzinfo=timezone.utc)
    cache._data = _fake_response()
    cache._slate_date = date(2026, 4, 28)
    cache._is_frozen = True
    cache._first_pitch_utc = fp
    cache._pipeline_failed = True

    cache.clear()

    assert cache._data is None
    assert cache._slate_date is None
    assert not cache._is_frozen
    assert cache._first_pitch_utc is None
    assert not cache._pipeline_failed


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------

def test_mark_failed_sets_flag():
    cache = _make_cache()
    assert not cache.pipeline_failed
    cache.mark_failed()
    assert cache.pipeline_failed


# ---------------------------------------------------------------------------
# set_schedule
# ---------------------------------------------------------------------------

def test_set_schedule_stores_first_pitch():
    cache = _make_cache()
    fp = datetime(2026, 4, 28, 23, 5, tzinfo=timezone.utc)
    cache.set_schedule(first_pitch_utc=fp)
    assert cache.first_pitch_utc == fp
