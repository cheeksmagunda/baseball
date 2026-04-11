"""
T-65 Sniper Architecture — event-driven slate monitor.

Four phases per slate day:

  1. INIT        — Morning pipeline runs on boot (handled by main.py startup task).
                   Builds internal data baseline; cache is NOT frozen yet.

  2. BEFORE_LOCK — Monitor sleeps until T-65 (65 min before first pitch).
                   /optimize returns HTTP 425 "come back later" with countdown.

  3. LOCKED      — At T-65 the monitor fires the final pipeline run, builds the
                   Starting 5 + Moonshot, then calls lineup_cache.freeze().
                   From this point the API serves a static payload — zero compute
                   per request, zero risk of dirty mid-run data.

  4. MONITORING  — Lightweight 60-second loop watching only for game completion.
                   On all-final: clear cache, pre-warm tomorrow's pipeline.

Design decisions
----------------
* 65-minute buffer = 60-min user draft window + 5-min generation headroom.
* Uses ZoneInfo("America/New_York") to parse "H:MM AM/PM ET" game times stored
  in SlateGame.scheduled_game_time. Automatically handles EDT vs EST via DST.
* If no scheduled_game_time values are present in the DB, falls back to the
  original status-polling approach (Preview→Live transition = freeze trigger).
  In fallback mode the "come back later" gate is skipped (no known lock time).
* Chunked async sleep (≤60 s per chunk) keeps CancelledError responsive.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# T-65: 60-min user draft window + 5-min final generation buffer
LOCK_MINUTES_BEFORE_PITCH = 65

# How often the post-lock loop checks for slate completion
POST_LOCK_CHECK_INTERVAL = 60  # seconds

_ET = ZoneInfo("America/New_York")
_STARTED_STATUSES = {"Live", "Final"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_game_time(game_time_str: str, game_date: date) -> datetime | None:
    """
    Parse a scheduled game time string into a UTC-aware datetime.

    Handles formats stored by fetch_schedule_for_date, e.g.:
      "7:05 PM ET"   "1:10 PM ET"   "10:10 AM PT"

    The suffix is stripped; the time is interpreted as America/New_York
    (handles EDT vs EST automatically via DST). Returns None on any parse
    failure so missing times never crash the monitor.
    """
    if not game_time_str:
        return None

    time_str = game_time_str.strip()
    for suffix in (" ET", " EST", " EDT", " CT", " MT", " PT"):
        if time_str.endswith(suffix):
            time_str = time_str[: -len(suffix)].strip()
            break

    try:
        naive_time = datetime.strptime(time_str, "%I:%M %p")
    except ValueError:
        try:
            naive_time = datetime.strptime(time_str, "%H:%M")
        except ValueError:
            logger.warning("Cannot parse game time string: %r", game_time_str)
            return None

    naive_dt = datetime(
        game_date.year, game_date.month, game_date.day,
        naive_time.hour, naive_time.minute, 0,
    )
    et_dt = naive_dt.replace(tzinfo=_ET)
    return et_dt.astimezone(timezone.utc)


def _get_first_pitch_utc(db, game_date: date) -> datetime | None:
    """
    Return the earliest scheduled game start time as a UTC datetime.

    Queries all SlateGame rows for the given date, parses
    scheduled_game_time via _parse_game_time, and returns the minimum.
    Returns None if no slate exists or no times can be parsed.
    """
    from app.models.slate import Slate, SlateGame

    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        return None

    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    times = []
    for g in games:
        if g.scheduled_game_time:
            parsed = _parse_game_time(g.scheduled_game_time, game_date)
            if parsed:
                times.append(parsed)

    return min(times) if times else None


async def _sleep_until(target: datetime) -> None:
    """
    Async sleep until a specific UTC datetime.

    Sleeps in ≤60-second chunks so asyncio.CancelledError is handled
    promptly without busy-waiting.
    """
    while True:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 60))


# ---------------------------------------------------------------------------
# Post-lock monitor (Phase 4)
# ---------------------------------------------------------------------------

async def _post_lock_monitor(today: date) -> None:
    """
    Lightweight completion watcher after the cache is frozen.

    Polls every POST_LOCK_CHECK_INTERVAL seconds. On slate completion
    (all games final) it clears the frozen cache and pre-warms tomorrow's
    pipeline. No lineup rebuilds — picks are locked.
    """
    from app.database import SessionLocal
    from app.models.slate import Slate, SlateGame
    from app.services.lineup_cache import lineup_cache
    from app.services.pipeline import run_full_pipeline
    from app.services.data_collection import fetch_schedule_for_date

    tomorrow_warmed = False

    logger.info("Post-lock monitor active — watching %s for completion", today)

    while True:
        try:
            await asyncio.sleep(POST_LOCK_CHECK_INTERVAL)

            db = SessionLocal()
            try:
                try:
                    await fetch_schedule_for_date(db, today)
                except Exception as exc:
                    logger.warning("Post-lock status refresh failed: %s", exc)

                slate = db.query(Slate).filter_by(date=today).first()
                if not slate:
                    continue

                games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
                if not games:
                    continue

                all_final = all(
                    g.home_score is not None and g.away_score is not None
                    for g in games
                )

                if not all_final:
                    continue

                logger.info(
                    "Slate %s complete (%d games final) — clearing frozen cache",
                    today, len(games),
                )
                lineup_cache.clear()

                if not tomorrow_warmed:
                    tomorrow = today + timedelta(days=1)
                    try:
                        logger.info("Pre-warming tomorrow's pipeline (%s)", tomorrow)
                        await run_full_pipeline(db, tomorrow)

                        from app.routers.filter_strategy import build_and_cache_lineups
                        cached = await build_and_cache_lineups(db)
                        if cached:
                            logger.info("Tomorrow's cache warmed (%s)", tomorrow)
                            tomorrow_warmed = True
                    except Exception as exc:
                        logger.warning(
                            "Tomorrow pre-warm failed — will retry next cycle: %s", exc
                        )

                if tomorrow_warmed:
                    logger.info("Post-lock monitor done for %s", today)
                    break

            finally:
                db.close()

        except asyncio.CancelledError:
            logger.info("Post-lock monitor cancelled")
            break
        except Exception as exc:
            logger.error("Post-lock monitor error (will retry): %s", exc)
            continue


# ---------------------------------------------------------------------------
# Fallback: status-polling monitor (no scheduled_game_time in DB)
# ---------------------------------------------------------------------------

async def _fallback_status_monitor(monitor_date: date | None = None) -> None:
    """
    Fallback monitor when scheduled_game_time is unavailable for all games.

    Replicates the old Preview→Live transition detection to find game starts.
    The cache is frozen at first-game-start (T-0), not T-65.

    The "come back later" gate is NOT active in this mode because there is
    no known lock_time_utc for the optimize endpoint to check against.
    """
    from app.database import SessionLocal
    from app.models.slate import Slate, SlateGame
    from app.services.lineup_cache import lineup_cache
    from app.services.pipeline import run_full_pipeline
    from app.services.data_collection import fetch_schedule_for_date

    today = monitor_date or date.today()
    prev_remaining: int | None = None
    frozen = False
    tomorrow_warmed = False

    logger.info(
        "Fallback status monitor active for %s "
        "(no scheduled_game_time found — will freeze at first game start)",
        today,
    )

    while True:
        try:
            await asyncio.sleep(30)

            db = SessionLocal()
            try:
                try:
                    await fetch_schedule_for_date(db, today)
                except Exception as exc:
                    logger.warning("Fallback monitor schedule refresh failed: %s", exc)

                slate = db.query(Slate).filter_by(date=today).first()
                if not slate:
                    prev_remaining = None
                    continue

                games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
                if not games:
                    continue

                remaining = sum(
                    1 for g in games if g.game_status not in _STARTED_STATUSES
                )

                # First game just started → run final pipeline and freeze
                if (
                    not frozen
                    and prev_remaining is not None
                    and remaining < prev_remaining
                ):
                    logger.info(
                        "First game started (fallback mode) — "
                        "running final pipeline and freezing cache"
                    )
                    try:
                        await run_full_pipeline(db, today)
                        from app.routers.filter_strategy import build_and_cache_lineups
                        cached = await build_and_cache_lineups(db)
                        if cached:
                            lineup_cache.freeze()
                            logger.info("Cache frozen at first game start (fallback)")
                            frozen = True
                    except Exception as exc:
                        logger.warning("Fallback freeze pipeline failed: %s", exc)

                prev_remaining = remaining

                all_final = all(
                    g.home_score is not None and g.away_score is not None
                    for g in games
                )

                if all_final and games:
                    lineup_cache.clear()
                    if not tomorrow_warmed:
                        tomorrow = today + timedelta(days=1)
                        try:
                            await run_full_pipeline(db, tomorrow)
                            from app.routers.filter_strategy import build_and_cache_lineups
                            cached = await build_and_cache_lineups(db)
                            if cached:
                                tomorrow_warmed = True
                        except Exception as exc:
                            logger.warning(
                                "Fallback tomorrow pre-warm failed: %s", exc
                            )
                    if tomorrow_warmed:
                        break

            finally:
                db.close()

        except asyncio.CancelledError:
            logger.info("Fallback monitor cancelled")
            break
        except Exception as exc:
            logger.error("Fallback monitor error (will retry): %s", exc)
            continue


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def targeted_slate_monitor(
    startup_done_event: asyncio.Event | None = None,
) -> None:
    """
    T-65 Sniper: event-driven slate monitor.

    Waits for the startup pipeline to complete, determines first-pitch time,
    sleeps until T-65, runs the final optimizer, freezes the cache, then
    switches to lightweight completion monitoring.

    Args:
        startup_done_event: Set by the startup pipeline task when it finishes.
                            If None the monitor proceeds immediately (useful
                            for testing or manual invocation).
    """
    from app.database import SessionLocal
    from app.services.lineup_cache import lineup_cache
    from app.services.pipeline import run_full_pipeline
    from app.routers.filter_strategy import build_and_cache_lineups

    # -----------------------------------------------------------------------
    # Wait for startup pipeline so we don't race with the morning init
    # -----------------------------------------------------------------------
    if startup_done_event is not None:
        logger.info("T-65 monitor waiting for startup pipeline to complete…")
        await startup_done_event.wait()
        logger.info("Startup pipeline done — T-65 monitor proceeding")

    # -----------------------------------------------------------------------
    # Phase 1: Determine the active slate date and first pitch time
    # -----------------------------------------------------------------------
    # Use the same active-date logic as the startup pipeline so the monitor
    # targets tomorrow's slate when today's games are already complete.
    db = SessionLocal()
    try:
        from app.routers.filter_strategy import _get_active_slate_date
        monitor_date = _get_active_slate_date(db)
        first_pitch_utc = _get_first_pitch_utc(db, monitor_date)
    finally:
        db.close()

    logger.info("T-65 monitor targeting date: %s", monitor_date)

    if first_pitch_utc is None:
        logger.warning(
            "No scheduled_game_time values found for %s — "
            "activating fallback status-polling monitor",
            monitor_date,
        )
        await _fallback_status_monitor(monitor_date)
        return

    lock_time_utc = first_pitch_utc - timedelta(minutes=LOCK_MINUTES_BEFORE_PITCH)

    # Publish timing so /status and /optimize can expose the countdown
    lineup_cache.set_schedule(first_pitch_utc=first_pitch_utc)

    logger.info(
        "T-%d schedule: first_pitch=%s UTC, lock=%s UTC (%.0f min from now)",
        LOCK_MINUTES_BEFORE_PITCH,
        first_pitch_utc.strftime("%H:%M"),
        lock_time_utc.strftime("%H:%M"),
        max(0, (lock_time_utc - datetime.now(timezone.utc)).total_seconds() / 60),
    )

    # -----------------------------------------------------------------------
    # Phase 2: Sleep until T-65
    # -----------------------------------------------------------------------
    now = datetime.now(timezone.utc)
    if now < lock_time_utc:
        logger.info(
            "Sleeping until T-%d (%s UTC)…",
            LOCK_MINUTES_BEFORE_PITCH,
            lock_time_utc.strftime("%H:%M"),
        )
        await _sleep_until(lock_time_utc)

    # -----------------------------------------------------------------------
    # Phase 3: Final pipeline run + cache freeze
    # -----------------------------------------------------------------------
    logger.info(
        "T-%d FINAL RUN — fetching data, building lineups, freezing cache",
        LOCK_MINUTES_BEFORE_PITCH,
    )

    db = SessionLocal()
    try:
        try:
            await run_full_pipeline(db, monitor_date)
            logger.info("T-%d pipeline complete", LOCK_MINUTES_BEFORE_PITCH)
        except Exception as exc:
            logger.error(
                "T-%d pipeline failed: %s — attempting lineup build with existing data",
                LOCK_MINUTES_BEFORE_PITCH, exc,
            )

        try:
            cached = await build_and_cache_lineups(db)
            if cached:
                lineup_cache.freeze(first_pitch_utc=first_pitch_utc)
                logger.info(
                    "Cache FROZEN. First pitch: %s UTC. Picks are locked.",
                    first_pitch_utc.strftime("%H:%M"),
                )
            else:
                logger.error(
                    "T-%d lineup build returned nothing — cache NOT frozen. "
                    "Falling back to status monitor.",
                    LOCK_MINUTES_BEFORE_PITCH,
                )
                await _fallback_status_monitor(monitor_date)
                return
        except Exception as exc:
            logger.error(
                "T-%d lineup build raised: %s — cache NOT frozen",
                LOCK_MINUTES_BEFORE_PITCH, exc,
            )
    finally:
        db.close()

    # -----------------------------------------------------------------------
    # Phase 4: Lightweight post-lock monitoring
    # -----------------------------------------------------------------------
    await _post_lock_monitor(monitor_date)
