"""
In-process lineup cache.

The startup pipeline pre-computes today's dual-lineup result once
(fetch → score → popularity → optimize) and stores it here.
The /optimize endpoint reads from this cache so the frontend gets
an instant response instead of triggering the full pipeline on every hit.

Cache is keyed by date so it automatically goes stale at midnight.
A Redis-backed version would be a drop-in replacement if multi-worker
or cross-restart persistence is needed.
"""

from datetime import date
from typing import Any, Optional


class _LineupCache:
    def __init__(self) -> None:
        self._data: Optional[Any] = None  # FilterOptimizeResponse
        self._date: Optional[date] = None

    def store(self, response: Any) -> None:
        self._data = response
        self._date = date.today()

    def get(self) -> Optional[Any]:
        """Return cached response if it was generated today, else None."""
        if self._data is not None and self._date == date.today():
            return self._data
        return None

    def clear(self) -> None:
        self._data = None
        self._date = None

    @property
    def is_warm(self) -> bool:
        return self.get() is not None


lineup_cache = _LineupCache()
