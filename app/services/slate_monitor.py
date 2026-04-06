"""
Background task for monitoring slate completion and cache invalidation.

Runs periodically (every 30 seconds by default) to check if today's slate
is complete. When all games have final scores, clears the lineup cache
and optionally triggers tomorrow's pipeline.
"""

import asyncio
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


async def monitor_slate_completion(check_interval: int = 30) -> None:
    """
    Background task: periodically check if the current slate is complete.

    When all games have final scores, clear the cache to force a rebuild
    on the next request. This ensures the frontend gets fresh lineups
    after the slate ends, without waiting for a manual request.

    Args:
        check_interval: seconds between checks (default 30)
    """
    from app.database import SessionLocal
    from app.models.slate import Slate, SlateGame
    from app.services.lineup_cache import lineup_cache
    from app.services.pipeline import run_full_pipeline

    while True:
        try:
            await asyncio.sleep(check_interval)

            # Check if today's slate is complete
            db = SessionLocal()
            try:
                today = date.today()
                slate = db.query(Slate).filter_by(date=today).first()

                if not slate:
                    # No slate for today yet
                    continue

                games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
                if not games:
                    # No games on slate
                    continue

                # Check if all games have final scores
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

                    # Trigger pipeline for tomorrow's slate (optional, non-blocking)
                    tomorrow = today + timedelta(days=1)
                    try:
                        logger.info("Triggering pipeline for tomorrow (%s)", tomorrow)
                        await run_full_pipeline(db, tomorrow)
                    except Exception as exc:
                        logger.warning("Tomorrow's pipeline failed (non-blocking): %s", exc)

            finally:
                db.close()

        except asyncio.CancelledError:
            logger.info("Slate monitor cancelled")
            break
        except Exception as exc:
            logger.error("Slate monitor error (will retry): %s", exc)
            # Don't crash — just log and continue
            continue
