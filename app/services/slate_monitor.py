"""
Background task for monitoring slate changes and keeping lineups fresh.

Runs every CHECK_INTERVAL seconds (default 30) and performs two jobs:

  **Fast loop (every check):**
    1. Refresh game statuses from MLB API (Preview → Live → Final)
    2. Detect game starts → rebuild cache with remaining games only
    3. Detect slate completion → clear cache, trigger next-day pipeline

  **Full refresh (every PIPELINE_REFRESH_INTERVAL, default 5 min):**
    4. Re-run the full pipeline (fetch starters, batting orders, stats, scores)
    5. Fingerprint the slate data — if anything changed, rebuild the lineup cache

This guarantees:
  - Lineups are regenerated cold on every deployment (handled by main.py lifespan)
  - Lineups are rebuilt within seconds of a game starting
  - Stale starters, batting orders, and boosts are picked up within 5 minutes
  - Tomorrow's pre-warm is retried until it succeeds

The app assumes MLB/API data is always available — no defensive retries
or exponential backoff.  If a fetch fails, the next cycle will pick it up.
"""

import asyncio
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_STARTED_STATUSES = {"Live", "Final"}

# How often the fast loop runs (game-start detection).
CHECK_INTERVAL_DEFAULT = 30

# How often the full pipeline re-runs (pick up new starters, orders, stats).
# Data is always available, so we can be aggressive.
PIPELINE_REFRESH_INTERVAL_DEFAULT = 300  # 5 minutes


def _slate_fingerprint(db) -> str:
    """Build a fingerprint of the active slate's key data fields.

    If any of these change, the lineup cache should be rebuilt:
      - Game starters (name + mlb_id)
      - Vegas lines / moneylines
      - Player card boosts
      - Batting orders
      - Game statuses
    """
    import hashlib
    from app.models.slate import Slate, SlateGame, SlatePlayer

    today = date.today()
    slate = db.query(Slate).filter_by(date=today).first()
    if not slate:
        return ""

    parts = []

    games = db.query(SlateGame).filter_by(slate_id=slate.id).order_by(SlateGame.id).all()
    for g in games:
        parts.append(
            f"G:{g.id}|{g.game_status}|"
            f"{g.home_starter}:{g.home_starter_mlb_id}|"
            f"{g.away_starter}:{g.away_starter_mlb_id}|"
            f"{g.vegas_total}|{g.home_moneyline}|{g.away_moneyline}|"
            f"{g.home_starter_era}|{g.away_starter_era}|"
            f"{g.home_starter_k_per_9}|{g.away_starter_k_per_9}|"
            f"{g.home_team_ops}|{g.away_team_ops}"
        )

    players = (
        db.query(SlatePlayer)
        .filter_by(slate_id=slate.id)
        .order_by(SlatePlayer.id)
        .all()
    )
    for p in players:
        parts.append(
            f"P:{p.id}|{p.card_boost}|{p.batting_order}|"
            f"{p.player_status}|{p.drafts}|{p.is_most_drafted_3x}"
        )

    raw = "\n".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def monitor_slate_completion(
    check_interval: int = CHECK_INTERVAL_DEFAULT,
    pipeline_refresh_interval: int = PIPELINE_REFRESH_INTERVAL_DEFAULT,
) -> None:
    """
    Background task: keep lineups fresh by reacting to slate changes.

    Fast loop (every check_interval seconds):
      - Refresh game statuses from MLB API
      - Game starts → rebuild cache with remaining games
      - All games final → clear cache, pre-warm tomorrow

    Full refresh (every pipeline_refresh_interval seconds):
      - Re-run full pipeline (starters, batting orders, stats, scores)
      - Compare slate fingerprint — rebuild cache only if data changed

    Args:
        check_interval: seconds between game-status checks (default 30)
        pipeline_refresh_interval: seconds between full pipeline refreshes (default 300)
    """
    from app.database import SessionLocal
    from app.models.slate import Slate, SlateGame
    from app.services.lineup_cache import lineup_cache
    from app.services.pipeline import run_full_pipeline
    from app.services.data_collection import fetch_schedule_for_date

    prev_remaining_count: int | None = None
    prev_fingerprint: str = ""
    seconds_since_refresh: int = 0
    tomorrow_warmed: bool = False

    while True:
        try:
            await asyncio.sleep(check_interval)
            seconds_since_refresh += check_interval

            db = SessionLocal()
            try:
                today = date.today()

                # -------------------------------------------------------
                # Fast loop: refresh game statuses and detect transitions
                # -------------------------------------------------------
                try:
                    await fetch_schedule_for_date(db, today)
                except Exception as exc:
                    logger.warning("Schedule refresh failed in monitor: %s", exc)

                slate = db.query(Slate).filter_by(date=today).first()
                if not slate:
                    prev_remaining_count = None
                    prev_fingerprint = ""
                    # No slate yet — try full pipeline to create one
                    if seconds_since_refresh >= pipeline_refresh_interval:
                        seconds_since_refresh = 0
                        try:
                            await run_full_pipeline(db, today)
                            logger.info("Pipeline created slate for %s", today)
                        except Exception as exc:
                            logger.warning("Pipeline for %s failed: %s", today, exc)
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
                        prev_fingerprint = _slate_fingerprint(db)
                    except Exception as exc:
                        logger.warning("Cache rebuild after game start failed: %s", exc)

                prev_remaining_count = remaining_count

                # -------------------------------------------------------
                # Check if all games are final (slate complete)
                # -------------------------------------------------------
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
                    prev_remaining_count = None
                    prev_fingerprint = ""

                    # Pre-warm tomorrow's pipeline (retry until successful)
                    if not tomorrow_warmed:
                        tomorrow = today + timedelta(days=1)
                        try:
                            logger.info("Triggering pipeline for tomorrow (%s)", tomorrow)
                            await run_full_pipeline(db, tomorrow)

                            from app.routers.filter_strategy import build_and_cache_lineups
                            cached = await build_and_cache_lineups(db)
                            if cached:
                                logger.info("Cache warmed for %s after slate turnover", tomorrow)
                                tomorrow_warmed = True
                        except Exception as exc:
                            logger.warning(
                                "Tomorrow's pipeline failed — will retry next cycle: %s", exc
                            )
                    continue

                # Reset tomorrow flag when we're back to a live slate day
                tomorrow_warmed = False

                # -------------------------------------------------------
                # Full refresh: re-run pipeline and check for data changes
                # -------------------------------------------------------
                if seconds_since_refresh >= pipeline_refresh_interval:
                    seconds_since_refresh = 0

                    try:
                        await run_full_pipeline(db, today)
                        logger.info("Periodic pipeline refresh complete for %s", today)
                    except Exception as exc:
                        logger.warning("Periodic pipeline refresh failed: %s", exc)
                        continue

                    # Check if slate data actually changed
                    new_fingerprint = _slate_fingerprint(db)
                    if new_fingerprint != prev_fingerprint:
                        logger.info(
                            "Slate data changed (fingerprint %s → %s) — rebuilding cache",
                            prev_fingerprint[:8] or "(empty)",
                            new_fingerprint[:8],
                        )
                        try:
                            from app.routers.filter_strategy import build_and_cache_lineups
                            cached = await build_and_cache_lineups(db)
                            if cached:
                                logger.info("Cache rebuilt after data change")
                            else:
                                logger.warning("Cache rebuild returned nothing after data change")
                        except Exception as exc:
                            logger.warning("Cache rebuild after data change failed: %s", exc)
                        prev_fingerprint = new_fingerprint
                    else:
                        logger.debug("Slate fingerprint unchanged — skipping cache rebuild")

            finally:
                db.close()

        except asyncio.CancelledError:
            logger.info("Slate monitor cancelled")
            break
        except Exception as exc:
            logger.error("Slate monitor error (will retry): %s", exc)
            continue
