"""
Persistent lineup cache.

Stores the most recent dual-lineup result in three tiers:
  1. In-process memory  — fastest, zero latency
  2. Redis              — survives restarts, shared across replicas (if configured)
  3. SQLite DB          — durable fallback when Redis is unavailable

On startup the cache is warm-loaded from Redis (or DB if Redis is absent)
so the first frontend request after a redeploy is always instant.

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
_REDIS_TTL = 86400  # 24 hours


class _LineupCache:
    def __init__(self) -> None:
        self._data: Optional[Any] = None          # FilterOptimizeResponse (Pydantic model)
        self._slate_date: Optional[date] = None   # date the cached slate belongs to
        self._redis: Optional[Any] = None         # redis.Redis instance, or None
        self._redis_checked: bool = False          # avoid re-attempting a failed connection
        self._is_frozen: bool = False             # True after T-65 freeze; blocks further writes
        self._first_pitch_utc: Optional[datetime] = None  # earliest game start (UTC)

    # ---------- Redis helpers ----------

    def _get_redis(self) -> Optional[Any]:
        """Return a live Redis client, or None if Redis is not configured / reachable."""
        if self._redis is not None:
            return self._redis
        if self._redis_checked:
            return None  # already tried and failed — don't retry on every call

        self._redis_checked = True
        from app.config import settings
        if not settings.redis_url:
            return None

        try:
            import redis as redis_lib
            client = redis_lib.from_url(settings.redis_url, decode_responses=True)
            client.ping()
            self._redis = client
            logger.info("Redis connected: %s", settings.redis_url)
        except Exception as exc:
            logger.warning("Redis unavailable — using DB cache only: %s", exc)

        return self._redis

    def _redis_key(self, slate_date: date) -> str:
        return f"{_REDIS_KEY_PREFIX}:{slate_date.isoformat()}"

    # ---------- T-65 schedule / freeze ----------

    def set_schedule(self, first_pitch_utc: datetime) -> None:
        """Store first-pitch time so /status can expose the T-65 countdown before the freeze."""
        self._first_pitch_utc = first_pitch_utc

    def freeze(self, first_pitch_utc: datetime | None = None) -> None:
        """
        Freeze the cache after the T-65 final run.

        From this point the cache is immutable — store() calls are no-ops
        until clear() resets state for the next day.
        """
        if first_pitch_utc is not None:
            self._first_pitch_utc = first_pitch_utc
        self._is_frozen = True
        logger.info("Lineup cache FROZEN — picks are locked until slate completion")

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

    # ---------- public API ----------

    def store(self, response: Any, slate_date: date | None = None) -> None:
        """Cache in memory, Redis, and SQLite DB."""
        if self._is_frozen:
            logger.debug("Cache is frozen — ignoring store() call (picks are locked)")
            return
        self._data = response
        self._slate_date = slate_date or date.today()
        payload = response.model_dump_json()

        # Tier 2 — Redis
        rc = self._get_redis()
        if rc is not None:
            try:
                rc.setex(self._redis_key(self._slate_date), _REDIS_TTL, payload)
                logger.info("Lineup cached in Redis for %s", self._slate_date)
            except Exception as exc:
                logger.warning("Redis write failed: %s", exc)

        # Tier 3 — SQLite (always write for durability)
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

    def restore_and_refreeze(self, first_pitch_utc: datetime) -> bool:
        """
        Attempt to warm-load today's cache from SQLite/Redis and immediately
        re-freeze it.  Called on startup when the app restarts during a live
        slate — prevents regenerating picks from a reduced candidate pool
        (started/final games already excluded) and serving different picks
        than the ones that were locked at T-65.

        Returns True if the cache was successfully restored and refrozen.
        """
        loaded = self.load_from_db()
        if not loaded:
            return False
        if self._slate_date != date.today():
            # Cache belongs to a different day — do not refreeze
            self._data = None
            self._slate_date = None
            return False
        self.freeze(first_pitch_utc=first_pitch_utc)
        logger.info(
            "Startup during live slate — restored frozen picks for %s "
            "(first pitch %s UTC). No regeneration.",
            self._slate_date,
            first_pitch_utc.strftime("%H:%M"),
        )
        return True

    def clear(self) -> None:
        self._data = None
        self._slate_date = None
        self._is_frozen = False
        self._first_pitch_utc = None

    def purge(self) -> None:
        """
        Wipe all three cache tiers (memory + Redis + SQLite DB).

        Called on startup so every redeploy starts from a clean state and
        the pipeline always regenerates fresh lineups rather than serving
        a cached result that may have been built with stale roster data.
        """
        self.clear()

        # Redis
        rc = self._get_redis()
        if rc is not None:
            try:
                # Delete today's key plus the previous day's key to be safe
                from datetime import timedelta
                for d in [date.today(), date.today() - timedelta(days=1)]:
                    rc.delete(self._redis_key(d))
                logger.info("Redis lineup cache purged")
            except Exception as exc:
                logger.warning("Redis purge failed: %s", exc)

        # SQLite
        from app.database import SessionLocal
        from app.models.slate import CachedLineup

        db = SessionLocal()
        try:
            db.query(CachedLineup).delete()
            db.commit()
            logger.info("DB lineup cache purged")
        except Exception as exc:
            logger.warning("DB purge failed: %s", exc)
            db.rollback()
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

        # Tier 2 — Redis (today's lineup)
        rc = self._get_redis()
        if rc is not None:
            try:
                data = rc.get(self._redis_key(date.today()))
                if data:
                    self._data = FilterOptimizeResponse.model_validate_json(data)
                    self._slate_date = date.today()
                    logger.info("Lineup cache loaded from Redis for %s", date.today())
                    return True
            except Exception as exc:
                logger.warning("Redis read failed on startup: %s", exc)

        # Tier 3 — SQLite
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
            logger.info("Lineup cache loaded from DB (slate date: %s)", row.cache_date)

            # Backfill Redis so subsequent restarts are faster
            if rc is not None:
                try:
                    rc.setex(self._redis_key(row.cache_date), _REDIS_TTL, row.response_json)
                    logger.info("Redis backfilled from DB for %s", row.cache_date)
                except Exception as exc:
                    logger.warning("Redis backfill failed: %s", exc)

            return True
        except Exception as exc:
            logger.warning("Failed to load lineup cache from DB: %s", exc)
            return False
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
        except Exception as exc:
            logger.warning("Failed to persist lineup cache: %s", exc)
            db.rollback()
        finally:
            db.close()


lineup_cache = _LineupCache()
