"""
Persistent lineup cache.

Stores the most recent dual-lineup result in three tiers:
  1. In-process memory  — fastest, zero latency
  2. Redis              — REQUIRED; survives restarts, source of truth for
                          freeze-state invariants (is_frozen, first_pitch_utc)
  3. SQLite DB          — durable record of the frozen payload

Redis is NOT optional. Startup validates DFS_REDIS_URL and raises if it
is unset or unreachable. After startup, any Redis failure also raises —
there is no silent degradation (per CLAUDE.md "No Fallbacks. Ever.").

On startup the cache is warm-loaded from Redis so the first frontend
request after a redeploy is always instant.

Turnover logic:
  - Before midnight → always serve (current slate is live).
  - After midnight  → check if every game on the cached slate is final.
    If yes, return None so the next request triggers the new-day pipeline.
    If no (late west-coast game), keep serving until it finishes.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REDIS_KEY_PREFIX = "lineup"
_REDIS_META_PREFIX = "lineup:meta"  # freeze-state invariants, survives restarts
_REDIS_TTL = 86400  # 24 hours


class _LineupCache:
    def __init__(self) -> None:
        self._data: Optional[Any] = None          # FilterOptimizeResponse (Pydantic model)
        self._slate_date: Optional[date] = None   # date the cached slate belongs to
        self._redis: Optional[Any] = None         # redis.Redis instance, or None
        self._redis_checked: bool = False          # avoid re-attempting a failed connection
        self._is_frozen: bool = False             # True after T-65 freeze; blocks further writes
        self._first_pitch_utc: Optional[datetime] = None  # earliest game start (UTC)
        self._pipeline_failed: bool = False       # True if T-65 pipeline raised an exception

    # ---------- Redis helpers ----------

    def _get_redis(self) -> Any:
        """Return a live Redis client. Raises if Redis is unconfigured or unreachable."""
        if self._redis is not None:
            return self._redis

        from app.config import settings
        if not settings.redis_url:
            raise RuntimeError(
                "DFS_REDIS_URL is not set. Redis is required — no DB-only fallback."
            )

        import redis as redis_lib
        client = redis_lib.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=5
        )
        client.ping()
        self._redis = client
        self._redis_checked = True
        import re as _re
        safe_url = _re.sub(r":([^@/]+)@", ":***@", settings.redis_url)
        logger.info("Redis connected: %s", safe_url)
        return self._redis

    def _redis_key(self, slate_date: date) -> str:
        return f"{_REDIS_KEY_PREFIX}:{slate_date.isoformat()}"

    def _redis_meta_key(self, slate_date: date) -> str:
        return f"{_REDIS_META_PREFIX}:{slate_date.isoformat()}"

    def _write_meta(self) -> None:
        """Mirror freeze-state invariants to Redis so restarts (and any sibling
        replica) see the same view. Called whenever _is_frozen or
        _first_pitch_utc changes."""
        if self._slate_date is None and self._first_pitch_utc is None:
            return
        import json
        slate_date = self._slate_date or date.today()
        payload = json.dumps({
            "frozen": self._is_frozen,
            "first_pitch_utc": self._first_pitch_utc.isoformat()
                if self._first_pitch_utc else None,
        })
        rc = self._get_redis()
        rc.setex(self._redis_meta_key(slate_date), _REDIS_TTL, payload)

    def _read_meta(self, slate_date: date) -> None:
        """Hydrate _is_frozen and _first_pitch_utc from Redis meta, if present."""
        import json
        rc = self._get_redis()
        raw = rc.get(self._redis_meta_key(slate_date))
        if not raw:
            return
        meta = json.loads(raw)
        self._is_frozen = bool(meta.get("frozen"))
        fp = meta.get("first_pitch_utc")
        if fp:
            self._first_pitch_utc = datetime.fromisoformat(fp)

    # ---------- T-65 schedule / freeze ----------

    def set_schedule(self, first_pitch_utc: datetime) -> None:
        """Store first-pitch time so /status can expose the T-65 countdown before the freeze."""
        self._first_pitch_utc = first_pitch_utc
        self._write_meta()

    def freeze(self, first_pitch_utc: datetime | None = None) -> None:
        """
        Freeze the cache after the T-65 final run.

        From this point the cache is immutable — store() calls are no-ops
        until clear() resets state for the next day. The frozen flag and
        first-pitch time are persisted to Redis so a restart (or any sibling
        replica) inherits the locked state without re-running the pipeline.
        """
        if first_pitch_utc is not None:
            self._first_pitch_utc = first_pitch_utc
        self._is_frozen = True
        self._write_meta()
        logger.info("Lineup cache FROZEN — picks are locked until slate completion")

    def mark_failed(self) -> None:
        """Signal that the T-65 pipeline raised an exception. /optimize returns 503."""
        self._pipeline_failed = True
        logger.error("Lineup cache marked FAILED — T-65 pipeline crashed, /optimize will return 503")

    @property
    def pipeline_failed(self) -> bool:
        return self._pipeline_failed

    def restore_and_refreeze(self, first_pitch_utc: datetime) -> bool:
        """
        Restore previously-frozen picks from persistent storage and re-freeze.

        Called on startup when T-65 has already passed for today's slate. Loads
        the payload from Redis/SQLite and the freeze-state from Redis meta so
        the monitor skips pipeline regeneration (which would fail because games
        may be Live/Final).

        Only restores if the cached slate date matches today — stale picks from
        a previous day are never served.

        Returns True if picks were successfully restored and re-frozen.
        """
        loaded = self.load_from_db()
        if not loaded:
            return False

        if self._slate_date != date.today():
            logger.warning(
                "Cached picks are from %s, not today (%s) — cannot restore",
                self._slate_date, date.today(),
            )
            self._data = None
            self._slate_date = None
            return False

        # load_from_db already hydrated _is_frozen / _first_pitch_utc from
        # Redis meta. Force-set the frozen flag here as a safety belt in case
        # meta was missing (e.g. Redis key evicted) but the payload survived.
        if not self._is_frozen:
            self._is_frozen = True
        if self._first_pitch_utc is None:
            self._first_pitch_utc = first_pitch_utc
        self._write_meta()
        logger.info(
            "Restored and re-frozen cached picks for %s (post-T-65 restart)",
            self._slate_date,
        )
        return True

    @property
    def is_frozen(self) -> bool:
        return self._is_frozen

    @property
    def first_pitch_utc(self) -> Optional[datetime]:
        return self._first_pitch_utc

    @property
    def lock_time_utc(self) -> Optional[datetime]:
        """T-65 lock target: 65 minutes (60-min window + 5-min generation buffer) before first pitch."""
        if self._first_pitch_utc is None:
            return None
        return self._first_pitch_utc - timedelta(minutes=65)

    @property
    def unlock_time_utc(self) -> Optional[datetime]:
        """T-60 unlock time: 60 minutes before first pitch. Picks become available after this."""
        if self._first_pitch_utc is None:
            return None
        return self._first_pitch_utc - timedelta(minutes=60)

    # ---------- public API ----------

    def store(self, response: Any, slate_date: date | None = None) -> None:
        """Cache in memory, Redis, and SQLite DB. Raises if Redis write fails."""
        if self._is_frozen:
            logger.debug("Cache is frozen — ignoring store() call (picks are locked)")
            return
        self._data = response
        self._slate_date = slate_date or date.today()
        payload = response.model_dump_json()

        # Tier 2 — Redis (required)
        rc = self._get_redis()
        rc.setex(self._redis_key(self._slate_date), _REDIS_TTL, payload)
        logger.info("Lineup cached in Redis for %s", self._slate_date)

        # Tier 3 — SQLite (durable record)
        self._persist(payload, self._slate_date)

    def get(self) -> Optional[Any]:
        """
        Return cached response if the slate is still active, else None.

        Before midnight the cached slate is always live.  After midnight
        we check whether every game on the slate has a final score; if so
        the slate is done and we let the caller rebuild for the new day.
        """
        if self._data is None:
            return None

        # Same calendar day → slate is definitely still active
        if self._slate_date is not None and self._slate_date >= date.today():
            return self._data

        # We've crossed midnight — check if the late games are finished
        if self._slate_is_complete():
            logger.info(
                "Slate %s complete (all games final) — clearing cache for new day",
                self._slate_date,
            )
            self._data = None
            self._slate_date = None
            return None

        # Late game still in progress — keep serving current picks
        return self._data


    def clear(self) -> None:
        self._data = None
        self._slate_date = None
        self._is_frozen = False
        self._first_pitch_utc = None
        self._pipeline_failed = False

    def purge(self) -> None:
        """
        Wipe all three cache tiers (memory + Redis + SQLite DB).

        Called on startup so every redeploy starts from a clean state and
        the pipeline always regenerates fresh lineups rather than serving
        a cached result that may have been built with stale roster data.
        """
        self.clear()

        # Redis (required) — wipe both the payload and the meta keys
        rc = self._get_redis()
        for d in [date.today(), date.today() - timedelta(days=1)]:
            rc.delete(self._redis_key(d), self._redis_meta_key(d))
        logger.info("Redis lineup cache purged")

        # SQLite
        from app.database import SessionLocal
        from app.models.slate import CachedLineup

        db = SessionLocal()
        try:
            db.query(CachedLineup).delete()
            db.commit()
            logger.info("DB lineup cache purged")
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @property
    def is_warm(self) -> bool:
        return self._data is not None

    # ---------- slate completion check ----------

    def _slate_is_complete(self) -> bool:
        """Check if every game on the cached slate has a final score."""
        if self._slate_date is None:
            return True

        from app.core.constants import NON_PLAYING_GAME_STATUSES
        from app.database import SessionLocal
        from app.models.slate import Slate, SlateGame

        db = SessionLocal()
        try:
            slate = db.query(Slate).filter_by(date=self._slate_date).first()
            if slate is None:
                return True

            games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
            if not games:
                return True

            # A postponed/cancelled/suspended game will never receive scores.
            # Treat those statuses as "done" so the cache doesn't perma-freeze.
            return all(
                (g.home_score is not None and g.away_score is not None)
                or g.game_status in NON_PLAYING_GAME_STATUSES
                for g in games
            )
        except Exception as exc:
            logger.error("Slate completion check failed: %s", exc)
            raise
        finally:
            db.close()

    # ---------- startup warm-load ----------

    def load_from_db(self) -> bool:
        """
        Warm-load cached response on startup — Redis first, then SQLite.

        Returns True if the cache was successfully populated.
        """
        from app.schemas.filter_strategy import FilterOptimizeResponse

        # Tier 2 — Redis (today's lineup). Redis is required; connection
        # failures raise. Missing key is fine (cache was purged).
        rc = self._get_redis()
        data = rc.get(self._redis_key(date.today()))
        if data:
            self._data = FilterOptimizeResponse.model_validate_json(data)
            self._slate_date = date.today()
            self._read_meta(self._slate_date)
            logger.info("Lineup cache loaded from Redis for %s", date.today())
            return True

        # Tier 3 — SQLite (durable record written alongside every Redis write)
        from app.database import SessionLocal
        from app.models.slate import CachedLineup

        db = SessionLocal()
        try:
            row = (
                db.query(CachedLineup)
                .order_by(CachedLineup.cache_date.desc())
                .first()
            )
            if row is None:
                return False

            self._data = FilterOptimizeResponse.model_validate_json(row.response_json)
            self._slate_date = row.cache_date
            self._read_meta(row.cache_date)
            logger.info("Lineup cache loaded from DB (slate date: %s)", row.cache_date)

            # Backfill Redis so subsequent restarts are faster.
            rc.setex(self._redis_key(row.cache_date), _REDIS_TTL, row.response_json)
            logger.info("Redis backfilled from DB for %s", row.cache_date)
            return True
        finally:
            db.close()

    # ---------- SQLite persistence ----------

    def _persist(self, payload: str, slate_date: date) -> None:
        """Write the serialized response to SQLite so it survives full restarts."""
        from app.database import SessionLocal
        from app.models.slate import CachedLineup

        db = SessionLocal()
        try:
            row = db.query(CachedLineup).filter_by(cache_date=slate_date).first()
            if row:
                row.response_json = payload
            else:
                db.add(CachedLineup(cache_date=slate_date, response_json=payload))
            db.commit()
            logger.info("Lineup cache persisted to DB for %s", slate_date)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


lineup_cache = _LineupCache()
