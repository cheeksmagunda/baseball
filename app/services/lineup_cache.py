"""
Persistent lineup cache.

Stores the most recent dual-lineup result both in-process (fast) and in
the database (survives restarts).  On startup the most recent DB row is
loaded so the first frontend request is instant.

The cache is NOT keyed on calendar date.  It serves whatever was last
stored and only gets replaced when the pipeline builds a new slate.
This handles late-night west-coast games that cross midnight — the
picks stay valid until a new slate is explicitly built.
"""

import logging
from datetime import date
from typing import Any, Optional

logger = logging.getLogger(__name__)


class _LineupCache:
    def __init__(self) -> None:
        self._data: Optional[Any] = None  # FilterOptimizeResponse (Pydantic model)

    # ---------- in-process (fast path) ----------

    def store(self, response: Any) -> None:
        """Cache in memory and persist to DB."""
        self._data = response
        self._persist(response)

    def get(self) -> Optional[Any]:
        """Return cached response, or None if empty."""
        return self._data

    def clear(self) -> None:
        self._data = None

    @property
    def is_warm(self) -> bool:
        return self._data is not None

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
            logger.info("Lineup cache loaded from DB (slate date: %s)", row.cache_date)
            return True
        except Exception as exc:
            logger.warning("Failed to load lineup cache from DB: %s", exc)
            return False
        finally:
            db.close()

    def _persist(self, response: Any) -> None:
        """Write the response to the database so it survives restarts."""
        from app.database import SessionLocal
        from app.models.slate import CachedLineup

        db = SessionLocal()
        try:
            today = date.today()
            row = db.query(CachedLineup).filter_by(cache_date=today).first()
            payload = response.model_dump_json()
            if row:
                row.response_json = payload
            else:
                db.add(CachedLineup(cache_date=today, response_json=payload))
            db.commit()
            logger.info("Lineup cache persisted to DB for %s", today)
        except Exception as exc:
            logger.warning("Failed to persist lineup cache: %s", exc)
            db.rollback()
        finally:
            db.close()


lineup_cache = _LineupCache()
