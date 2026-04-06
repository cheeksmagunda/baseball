"""
Persistent lineup cache.

Stores the most recent dual-lineup result both in-process (fast) and in
the database (survives restarts).  On startup the most recent DB row is
loaded so the first frontend request is instant.

Turnover logic:
  - Before midnight → always serve (current slate is live).
  - After midnight  → check if every game on the cached slate is final.
    If yes, return None so the next request triggers the new-day pipeline.
    If no (late west-coast game), keep serving until it finishes.
"""

import logging
from datetime import date
from typing import Any, Optional

logger = logging.getLogger(__name__)


class _LineupCache:
    def __init__(self) -> None:
        self._data: Optional[Any] = None  # FilterOptimizeResponse (Pydantic model)
        self._slate_date: Optional[date] = None  # date the cached slate belongs to

    # ---------- in-process (fast path) ----------

    def store(self, response: Any, slate_date: date | None = None) -> None:
        """Cache in memory and persist to DB."""
        self._data = response
        self._slate_date = slate_date or date.today()
        self._persist(response, self._slate_date)

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

    @property
    def is_warm(self) -> bool:
        return self._data is not None

    # ---------- slate completion check ----------

    def _slate_is_complete(self) -> bool:
        """
        Check if every game on the cached slate has a final score.

        Only called after midnight, so the extra DB hit is rare.
        """
        if self._slate_date is None:
            return True  # no slate info → treat as stale

        from app.database import SessionLocal
        from app.models.slate import Slate, SlateGame

        db = SessionLocal()
        try:
            slate = db.query(Slate).filter_by(date=self._slate_date).first()
            if slate is None:
                return True  # slate gone → stale

            games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
            if not games:
                return True  # no games → stale

            # A game is final when both scores are populated
            return all(
                g.home_score is not None and g.away_score is not None
                for g in games
            )
        except Exception as exc:
            logger.warning("Slate completion check failed: %s", exc)
            return False  # on error, keep serving (safe default)
        finally:
            db.close()

    # ---------- DB persistence ----------

    def load_from_db(self) -> bool:
        """
        Load the most recent cached response from the database.

        Called once at startup so the frontend gets instant picks
        even after a redeploy.  Returns True if the cache was loaded.
        """
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

            from app.schemas.filter_strategy import FilterOptimizeResponse
            self._data = FilterOptimizeResponse.model_validate_json(row.response_json)
            self._slate_date = row.cache_date
            logger.info("Lineup cache loaded from DB (slate date: %s)", row.cache_date)
            return True
        except Exception as exc:
            logger.warning("Failed to load lineup cache from DB: %s", exc)
            return False
        finally:
            db.close()

    def _persist(self, response: Any, slate_date: date) -> None:
        """Write the response to the database so it survives restarts."""
        from app.database import SessionLocal
        from app.models.slate import CachedLineup

        db = SessionLocal()
        try:
            row = db.query(CachedLineup).filter_by(cache_date=slate_date).first()
            payload = response.model_dump_json()
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
