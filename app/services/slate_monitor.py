"""
Background task for monitoring slate completion and cache invalidation.

Runs periodically (every 30 seconds by default) to:
  1. Refresh game statuses from the MLB API (Preview → Live → Final)
  2. Rebuild the lineup cache when a game starts (remaining games only)
  3. Clear the cache and pre-warm tomorrow's pipeline when all games finish
"""

import asyncio
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_STARTED_STATUSES = {"Live", "Final"}


async def monitor_slate_completion(check_interval: int = 30) -> None:
    """
    Background task: periodically refresh game statuses and react to changes.

    - When a game starts (Preview → Live): rebuild cache with remaining games.
    - When all games are final: clear cache and pre-warm tomorrow's pipeline.

    Args:
        check_interval: seconds between checks (default 30)
    """
    from app.database import SessionLocal
    from app.models.slate import Slate, SlateGame
    from app.services.lineup_cache import lineup_cache
    from app.services.pipeline import run_full_pipeline
    from app.services.data_collection import fetch_schedule_for_date

    prev_remaining_count: int | None = None

    while True:
        try:
            await asyncio.sleep(check_interval)

            db = SessionLocal()
            try:
                today = date.today()

                # Refresh game statuses from MLB API so we detect Preview → Live transitions.
                try:
                    await fetch_schedule_for_date(db, today)
                except Exception as exc:
                    logger.warning("Schedule refresh failed in monitor: %s", exc)

                slate = db.query(Slate).filter_by(date=today).first()
                if not slate:
                    prev_remaining_count = None
                    continue

                games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
                if not games:
                    prev_remaining_count = None
                    continue

                remaining_count = sum(
                    1 for g in games if g.game_status not in _STARTED_STATUSES
                )

                # A game just started — rebuild cache with remaining games only.
                if (
                    prev_remaining_count is not None
                    and remaining_count < prev_remaining_count
                    and remaining_count > 0
                ):
                    logger.info(
                        "Game started — rebuilding cache with %d remaining game(s)",
                        remaining_count,
                    )
                    try:
                        from app.routers.filter_strategy import build_and_cache_lineups
                        cached = await build_and_cache_lineups(db)
                        if cached:
                            logger.info("Cache rebuilt for %d remaining game(s)", remaining_count)
                        else:
                            logger.warning("Cache rebuild returned nothing (no eligible candidates?)")
                    except Exception as exc:
                        logger.warning("Cache rebuild after game start failed: %s", exc)

                prev_remaining_count = remaining_count

                # Check if all games have final scores (slate complete).
                all_final = all(
                    g.home_score is not None and g.away_score is not None
                    for g in games
                )

                if all_final:
                    logger.info(
                        "Slate %s complete (all %d games final) — clearing cache",
                        today,
                        len(games),
                    )
                    lineup_cache.clear()
                    prev_remaining_count = None  # reset for next day

                    # Pre-warm tomorrow's pipeline
                    tomorrow = today + timedelta(days=1)
                    try:
                        logger.info("Triggering pipeline for tomorrow (%s)", tomorrow)
                        await run_full_pipeline(db, tomorrow)

                        from app.routers.filter_strategy import build_and_cache_lineups
                        cached = await build_and_cache_lineups(db)
                        if cached:
                            logger.info("Cache warmed for %s after slate turnover", tomorrow)
                    except Exception as exc:
                        logger.warning("Tomorrow's pipeline failed (non-blocking): %s", exc)

            finally:
                db.close()

        except asyncio.CancelledError:
            logger.info("Slate monitor cancelled")
            break
        except Exception as exc:
            logger.error("Slate monitor error (will retry): %s", exc)
            continue
