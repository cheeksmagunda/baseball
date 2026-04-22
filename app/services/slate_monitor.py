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

After Phase 4 completes for a slate, the monitor loops back to Phase 1 to
target the next slate day. This ensures multi-day uptime without requiring
app restarts to re-trigger the T-65 cycle.

Design decisions
----------------
* 65-minute buffer = 60-min user draft window + 5-min generation headroom.
* Uses ZoneInfo("America/New_York") to parse "H:MM AM/PM ET" game times stored
  in SlateGame.scheduled_game_time. Automatically handles EDT vs EST via DST.
* Raises RuntimeError if no scheduled_game_time values are present — there is
  no fallback mode.  Missing game times are a critical error requiring investigation.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_game_time(game_time_str: str, game_date: date) -> datetime | None:
    """
    Parse a scheduled game time string into a UTC-aware datetime.

    Input is always "H:MM AM/PM ET" as written by _format_game_time_et().
    Returns None only for null/empty input (game has no scheduled time).
    """
    if not game_time_str:
        return None

    time_str = game_time_str.strip()
    for suffix in (" ET", " EST", " EDT", " CT", " MT", " PT"):
        if time_str.endswith(suffix):
            time_str = time_str[: -len(suffix)].strip()
            break

    naive_time = datetime.strptime(time_str, "%I:%M %p")

    # Late West Coast games (e.g. 10:10 PM PT) convert to early-AM ET times
    # (1:10 AM ET) that belong to the *next* calendar day.  Without this
    # correction the converted UTC timestamp falls before all afternoon games,
    # making min(times) fire the T-65 lock at midnight — 12+ hours too early.
    # No MLB game intentionally starts between midnight and 5 AM ET, so any
    # sub-5 AM time safely belongs to the following calendar day.
    actual_date = game_date
    if naive_time.hour < 5:
        actual_date = game_date + timedelta(days=1)

    naive_dt = datetime(
        actual_date.year, actual_date.month, actual_date.day,
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
# Pre-T-65 Statcast refresh (fires inside Phase 2 as a background task)
# ---------------------------------------------------------------------------

async def _refresh_statcast_background() -> None:
    """Bulk-load Baseball Savant leaderboards into PlayerStats.

    Fires as a detached asyncio task unconditionally on every slate cycle.
    Savant is fully public and always live — failure is a hard stop, not a
    graceful degradation.  On any failure the cache is marked failed so
    /optimize returns 503 instead of serving lineups built on stale kinematics.
    """
    from app.services.lineup_cache import lineup_cache

    try:
        from scripts.refresh_statcast import main as refresh_main

        logger.info("Statcast refresh: starting bulk load from Baseball Savant")
        exit_code = await asyncio.to_thread(refresh_main)
        if exit_code != 0:
            logger.critical(
                "Statcast refresh exited with code=%d — marking pipeline failed. "
                "Savant is public and always live; a non-zero exit means a real "
                "connectivity or schema problem that must be fixed.",
                exit_code,
            )
            lineup_cache.mark_failed()
        else:
            logger.info("Statcast refresh complete (exit=0)")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.critical(
            "Statcast refresh raised — marking pipeline failed.",
            exc_info=True,
        )
        lineup_cache.mark_failed()


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
    from app.core.constants import NON_PLAYING_GAME_STATUSES
    from app.database import SessionLocal
    from app.models.slate import Slate, SlateGame
    from app.services.lineup_cache import lineup_cache
    from app.services.data_collection import fetch_schedule_for_date

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
                    (g.home_score is not None and g.away_score is not None)
                    or g.game_status in NON_PLAYING_GAME_STATUSES
                    for g in games
                )

                if not all_final:
                    continue

                logger.info(
                    "Slate %s complete (%d games final) — clearing frozen cache",
                    today, len(games),
                )
                lineup_cache.clear()

                # Fetch tomorrow's schedule so Phase 1 of the next cycle has
                # game times immediately (countdown shows right after restart).
                # Best-effort only — Phase 1 re-fetches if this fails.
                tomorrow = today + timedelta(days=1)
                try:
                    await fetch_schedule_for_date(db, tomorrow)
                    logger.info("Tomorrow's schedule pre-fetched (%s)", tomorrow)
                except Exception as exc:
                    logger.warning(
                        "Tomorrow schedule pre-fetch failed — Phase 1 will retry: %s", exc
                    )

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
# Main entry point
# ---------------------------------------------------------------------------

async def targeted_slate_monitor(
    startup_done_event: asyncio.Event | None = None,
) -> None:
    """
    T-65 Sniper: event-driven slate monitor.

    Waits for the startup pipeline to complete, then enters a perpetual loop:
    determines first-pitch time, sleeps until T-65, runs the final optimizer,
    freezes the cache, then switches to lightweight completion monitoring.
    After each slate completes, loops back to target the next slate day.

    Args:
        startup_done_event: Set by the startup pipeline task when it finishes.
                            If None the monitor proceeds immediately (useful
                            for testing or manual invocation).
    """
    from app.database import SessionLocal
    from app.services.lineup_cache import lineup_cache
    from app.services.pipeline import run_full_pipeline
    from app.routers.filter_strategy import build_and_cache_lineups
    from app.services.data_collection import fetch_schedule_for_date
    from app.routers.filter_strategy import _get_active_slate_date

    # -----------------------------------------------------------------------
    # Wait for startup pipeline so we don't race with the morning init
    # -----------------------------------------------------------------------
    if startup_done_event is not None:
        logger.info("T-65 monitor waiting for startup pipeline to complete…")
        await startup_done_event.wait()
        logger.info("Startup pipeline done — T-65 monitor proceeding")

    # -----------------------------------------------------------------------
    # One slate cycle: Phase 1 → Phase 4.  Extracted so the perpetual loop
    # below can wrap it in top-level error handling — any uncaught exception
    # used to silently kill the monitor task, leaving /optimize returning
    # HTTP 425 "generating" forever because is_frozen stayed False and
    # pipeline_failed stayed False.
    # -----------------------------------------------------------------------
    async def _run_slate_cycle() -> None:
        # -------------------------------------------------------------------
        # Phase 1: Determine the active slate date and first pitch time
        # -------------------------------------------------------------------
        # Use the same active-date logic as the startup pipeline so the
        # monitor targets tomorrow's slate when today's games are already
        # complete.
        #
        # Bootstrap: the monitor cannot sleep until T-65 without knowing when
        # first pitch is, and that requires SlateGame.scheduled_game_time rows
        # in the DB. On a fresh deploy, no Slate row exists for today. Fetch
        # the schedule here — this is the ONE MLB API call allowed outside
        # T-65, because without it the monitor has no idea when T-65 is.
        #
        # Deploy timing determines what happens next:
        #   pre-T-65:       main.py purged cache; Phase 2 sleeps until T-65
        #   T-65 window:    main.py purged cache; Phase 2 skips sleep (lock
        #                   already past); Phase 3 runs fresh cold pipeline
        #   post-first-pitch: main.py restored+refroze cache; Phase 3's
        #                   is_frozen guard fires and skips pipeline entirely

        today = date.today()
        db = SessionLocal()
        _phase1_error: Exception | None = None
        monitor_date: date = today
        first_pitch_utc = None
        try:
            # Use _get_first_pitch_utc as the "do we know T-65?" probe.
            # fetch_schedule_for_date is idempotent (upserts), so it's safe
            # to call whether the Slate row is missing OR present-but-empty.
            if _get_first_pitch_utc(db, today) is None:
                logger.info("Monitor bootstrap: fetching schedule for %s", today)
                try:
                    await fetch_schedule_for_date(db, today)
                except Exception as exc:
                    _phase1_error = exc

            if _phase1_error is None:
                monitor_date = _get_active_slate_date(db)
                first_pitch_utc = _get_first_pitch_utc(db, monitor_date)

                # If _get_active_slate_date targeted tomorrow (today's slate
                # empty or all-final), make sure tomorrow's schedule is
                # populated too.
                if first_pitch_utc is None and monitor_date != today:
                    logger.info(
                        "Monitor bootstrap: fetching schedule for %s", monitor_date
                    )
                    try:
                        await fetch_schedule_for_date(db, monitor_date)
                    except Exception as exc:
                        _phase1_error = exc
                    else:
                        first_pitch_utc = _get_first_pitch_utc(db, monitor_date)
        finally:
            db.close()

        if _phase1_error is not None:
            logger.error(
                "Monitor bootstrap: schedule fetch failed (%s) — retrying in 5 min",
                _phase1_error,
            )
            await asyncio.sleep(300)
            return

        logger.info("T-65 monitor targeting date: %s", monitor_date)

        if first_pitch_utc is None:
            logger.critical(
                "No scheduled_game_time values found for %s — retrying in 5 min. "
                "See DISASTER_RECOVERY.md § Scenario 1 for debugging steps.",
                monitor_date,
            )
            await asyncio.sleep(300)
            return

        lock_time_utc = first_pitch_utc - timedelta(
            minutes=LOCK_MINUTES_BEFORE_PITCH
        )

        # Publish timing so /status and /optimize can expose the countdown
        lineup_cache.set_schedule(first_pitch_utc=first_pitch_utc)

        logger.info(
            "T-%d schedule: first_pitch=%s UTC, lock=%s UTC (%.0f min from now)",
            LOCK_MINUTES_BEFORE_PITCH,
            first_pitch_utc.strftime("%H:%M"),
            lock_time_utc.strftime("%H:%M"),
            max(
                0,
                (lock_time_utc - datetime.now(timezone.utc)).total_seconds()
                / 60,
            ),
        )

        # ---------------------------------------------------------------
        # Phase 2: Pre-lock Statcast refresh + sleep until T-65
        # ---------------------------------------------------------------
        # Kick the Statcast refresh BEFORE the sleep guard so a cold start
        # that completes at or past T-65 still loads fresh Savant data.
        # If startup was slow (Redis retries, migrations) and T-65 already
        # passed by the time the monitor reaches here, `now < lock_time_utc`
        # would be False — skipping the entire block — and the refresh would
        # never fire.  Hoisting it out of the conditional fixes that: the
        # background task races the T-65 pipeline and the pipeline reads
        # whatever has been persisted when it fires (NULL → non-Statcast
        # fallback path if the ~60 s bulk-load isn't done yet).
        # Always unconditional — Savant is fully public and always live.
        asyncio.create_task(_refresh_statcast_background())

        now = datetime.now(timezone.utc)
        if now < lock_time_utc:
            logger.info(
                "Sleeping until T-%d (%s UTC)…",
                LOCK_MINUTES_BEFORE_PITCH,
                lock_time_utc.strftime("%H:%M"),
            )
            await _sleep_until(lock_time_utc)

        # ---------------------------------------------------------------
        # Phase 3: Final pipeline run + cache freeze
        # ---------------------------------------------------------------

        # If the cache is already frozen (e.g., this monitor task is
        # re-entering after previous T-65 run), skip the final pipeline
        # run entirely. The frozen picks are already locked and valid for
        # the current slate. Re-running the pipeline with started/final
        # games excluded would produce a different candidate pool, risking
        # inconsistency.
        if lineup_cache.is_frozen:
            logger.info(
                "T-%d monitor: cache already frozen (restart during live "
                "slate) — skipping final pipeline run, proceeding to "
                "post-lock monitoring.",
                LOCK_MINUTES_BEFORE_PITCH,
            )
        else:
            # ---------------------------------------------------------------
            # Phase 2b: Refresh the schedule right before the final run so
            # weather delays push the lock *back* instead of locking early
            # on stale times. Loop until the newly-parsed first pitch is
            # within tolerance of the cached one; if it moves later,
            # re-sleep to the new T-65.
            # ---------------------------------------------------------------
            while True:
                db_refresh = SessionLocal()
                refreshed_first_pitch = None
                try:
                    await fetch_schedule_for_date(db_refresh, monitor_date)
                    refreshed_first_pitch = _get_first_pitch_utc(
                        db_refresh, monitor_date
                    )
                except Exception:
                    logger.exception(
                        "T-%d Phase 2b schedule refresh failed — marking "
                        "pipeline failed; /optimize will return 503.",
                        LOCK_MINUTES_BEFORE_PITCH,
                    )
                    lineup_cache.mark_failed()
                    return
                finally:
                    db_refresh.close()

                if refreshed_first_pitch is None:
                    logger.critical(
                        "No scheduled_game_time values for %s at T-%d — "
                        "marking pipeline failed; /optimize will return 503.",
                        monitor_date,
                        LOCK_MINUTES_BEFORE_PITCH,
                    )
                    lineup_cache.mark_failed()
                    return

                # <2 min drift is noise (schedule-string rounding); treat
                # as stable.
                if refreshed_first_pitch <= first_pitch_utc + timedelta(
                    minutes=2
                ):
                    break

                logger.warning(
                    "First pitch pushed back: %s UTC -> %s UTC — "
                    "re-sleeping to new T-%d",
                    first_pitch_utc.strftime("%H:%M"),
                    refreshed_first_pitch.strftime("%H:%M"),
                    LOCK_MINUTES_BEFORE_PITCH,
                )
                first_pitch_utc = refreshed_first_pitch
                lock_time_utc = first_pitch_utc - timedelta(
                    minutes=LOCK_MINUTES_BEFORE_PITCH
                )
                lineup_cache.set_schedule(first_pitch_utc=first_pitch_utc)
                await _sleep_until(lock_time_utc)

            logger.info(
                "T-%d FINAL RUN — fetching data, building lineups, "
                "freezing cache",
                LOCK_MINUTES_BEFORE_PITCH,
            )

            db = SessionLocal()
            try:
                try:
                    await run_full_pipeline(db, monitor_date)
                    logger.info(
                        "T-%d pipeline complete", LOCK_MINUTES_BEFORE_PITCH
                    )

                    cached = await build_and_cache_lineups(
                        db, slate_date=monitor_date
                    )
                    if cached:
                        # freeze() is inside the try so a transient Redis
                        # hiccup at freeze time marks failed (503) instead
                        # of killing the monitor task (which would leave
                        # /optimize returning 425 "generating" forever).
                        lineup_cache.freeze(first_pitch_utc=first_pitch_utc)
                        logger.info(
                            "Cache FROZEN. First pitch: %s UTC. Picks are locked.",
                            first_pitch_utc.strftime("%H:%M"),
                        )
                    else:
                        lineup_cache.mark_failed()
                        logger.error(
                            "T-%d lineup build returned nothing — no slate data "
                            "available. /optimize will return 503.",
                            LOCK_MINUTES_BEFORE_PITCH,
                        )
                except Exception:
                    logger.exception(
                        "T-%d PIPELINE FAILED — see traceback below. "
                        "No fallback; /optimize will return 503.",
                        LOCK_MINUTES_BEFORE_PITCH,
                    )
                    lineup_cache.mark_failed()
            finally:
                db.close()

        # ---------------------------------------------------------------
        # Phase 4: Lightweight post-lock monitoring
        # ---------------------------------------------------------------
        await _post_lock_monitor(monitor_date)

        # Slate complete — loop back to Phase 1 for the next slate day.
        logger.info(
            "Slate %s cycle complete — restarting monitor for next slate",
            monitor_date,
        )

    # -----------------------------------------------------------------------
    # Perpetual slate loop with top-level resilience.
    #
    # Any uncaught exception inside a cycle (Redis hiccup during
    # set_schedule/freeze, DB outage, unexpected logic error) used to
    # silently kill the monitor task.  The /optimize endpoint would then
    # return HTTP 425 "generating" forever (is_frozen=False,
    # pipeline_failed=False), and the frontend would poll indefinitely.
    # We now catch, mark the cache failed so /optimize returns 503
    # immediately, pause briefly, and restart the cycle.
    # -----------------------------------------------------------------------
    while True:
        try:
            await _run_slate_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.critical(
                "T-65 monitor: unhandled exception escaped slate cycle — "
                "marking pipeline failed so /optimize returns 503 instead "
                "of spinning on 425. Restarting cycle in 60s.",
                exc_info=True,
            )
            try:
                lineup_cache.mark_failed()
            except Exception:
                logger.exception(
                    "mark_failed() itself raised — continuing anyway"
                )
            await asyncio.sleep(60)
