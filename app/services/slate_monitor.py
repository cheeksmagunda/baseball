"""
T-65 Sniper Architecture — event-driven slate monitor.

Four phases per slate day:

  1. INIT        — Morning pipeline runs on boot (handled by main.py startup task).
                   Builds internal data baseline; cache is NOT frozen yet.

  2. BEFORE_LOCK — Monitor sleeps until T-65 (65 min before first pitch).
                   /optimize returns HTTP 425 "come back later" with countdown.

  3. LOCKED      — At T-65 the monitor fires the final pipeline run, builds the
                   lineup, then calls lineup_cache.freeze().
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


def _save_app_picks(db, today: date) -> None:
    """Append the frozen lineup's 5 picks to data/historical_app_picks.csv.

    Called once per slate just before lineup_cache.purge() wipes CachedLineup
    from SQLite.  real_score and box-stat columns are written blank — fill
    real_score manually after the platform posts results; run
    scripts/backfill_slate_results_and_hv_stats.py to auto-populate box stats.
    """
    import csv
    from pathlib import Path
    from app.models.slate import CachedLineup
    from app.schemas.filter_strategy import FilterOptimizeResponse

    row = db.query(CachedLineup).filter_by(cache_date=today).first()
    if row is None:
        logger.warning("_save_app_picks: no CachedLineup for %s — skipping", today)
        return

    try:
        response = FilterOptimizeResponse.model_validate_json(row.response_json)
    except Exception as exc:
        logger.error("_save_app_picks: failed to parse cached picks for %s: %s", today, exc)
        return

    csv_path = Path(__file__).resolve().parents[2] / "data" / "historical_app_picks.csv"
    fieldnames = [
        "date", "slot_index", "player_name", "team", "position",
        "slot_mult", "filter_ev", "env_score", "total_score", "real_score",
        "ab", "r", "h", "hr", "rbi", "bb", "so",
        "ip", "er", "k_pitching", "decision",
    ]

    write_header = not csv_path.exists()
    with csv_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for slot in response.lineup.lineup:
            writer.writerow({
                "date": today.isoformat(),
                "slot_index": slot.slot_index,
                "player_name": slot.player_name,
                "team": slot.team,
                "position": slot.position,
                "slot_mult": slot.slot_mult,
                "filter_ev": round(slot.filter_ev, 4),
                "env_score": round(slot.env_score, 4),
                "total_score": round(slot.total_score, 2),
                "real_score": "",
                "ab": "", "r": "", "h": "", "hr": "", "rbi": "", "bb": "", "so": "",
                "ip": "", "er": "", "k_pitching": "", "decision": "",
            })

    logger.info("App picks for %s appended to %s", today, csv_path.name)


# T-65: 60-min user draft window + 5-min final generation buffer
LOCK_MINUTES_BEFORE_PITCH = 65

# How often the post-lock loop checks for slate completion AFTER any game
# could plausibly be final.  Before that point we sleep — no API calls.
POST_LOCK_CHECK_INTERVAL = 120  # seconds

# Earliest time a single MLB game can finish (no extras / no rain delay).
# Used by the post-lock monitor to know how long to sleep AFTER first
# pitch before it's worth polling for completion.  2.5 hours is below the
# 9-inning floor (~2:35 average over the last decade) so we'd never miss
# a finished game by sleeping this long.
MIN_GAME_DURATION_MINUTES = 150  # 2.5 hours

# Once a game has been live this long, even with extras + rain it should
# be final.  Used as the polling cadence ceiling — we never sleep longer
# than this between completion checks.
MAX_GAME_DURATION_MINUTES = 270  # 4.5 hours

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
    """Return the slate's TRUE earliest scheduled first pitch.

    This is a stable slate-level timestamp — it does NOT change as
    games progress from Preview to Live to Final.  Used to compute
    `lock_time = first_pitch − 65 min` and the "is the slate live"
    boundary.  Both must be invariant across mid-slate restarts:
    if first_pitch were redefined as "earliest REMAINING game", a
    redeploy at 17:57 on a slate whose 18:20 game is already Live
    would compute a fake first_pitch=19:10 and a fake lock_time=18:05
    in the future, causing the monitor to sleep instead of restoring
    frozen picks or running the cold pipeline on remaining games.

    Postponed/Cancelled/Suspended games are excluded — those scheduled
    times are no longer on the slate. Live and Final games ARE
    included because their scheduled first pitch is a real, immutable
    point in time that defines the slate boundary.

    Returns None if no slate exists or no times can be parsed.
    """
    from app.core.constants import NON_PLAYING_GAME_STATUSES
    from app.models.slate import Slate, SlateGame

    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        return None

    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    times = []
    for g in games:
        if g.game_status in NON_PLAYING_GAME_STATUSES:
            continue
        if not g.scheduled_game_time:
            raise RuntimeError(
                f"Game {g.home_team} vs {g.away_team} ({g.game_status}) has no "
                "scheduled_game_time — schedule fetch returned incomplete data. "
                "Cannot compute T-65 lock time without all game times."
            )
        parsed = _parse_game_time(g.scheduled_game_time, game_date)
        if parsed:
            times.append(parsed)

    return min(times) if times else None


def _get_last_first_pitch_utc(db, game_date: date) -> datetime | None:
    """Latest scheduled first pitch on the slate.

    No game can be Final before its own first pitch.  The post-lock
    monitor uses this to schedule its first completion check at
    `last_first_pitch + MIN_GAME_DURATION_MINUTES` — earlier polls
    are guaranteed to return "still live", so they'd be wasted MLB
    API calls.
    """
    from app.models.slate import Slate, SlateGame

    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        return None

    from app.core.constants import NON_PLAYING_GAME_STATUSES

    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    times = []
    for g in games:
        if g.game_status in NON_PLAYING_GAME_STATUSES:
            continue
        if not g.scheduled_game_time:
            continue
        parsed = _parse_game_time(g.scheduled_game_time, game_date)
        if parsed:
            times.append(parsed)

    return max(times) if times else None


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
    """State-aware completion watcher after the cache is frozen.

    The slate has known boundaries: it cannot END before
    `last_first_pitch + MIN_GAME_DURATION_MINUTES`, and any single game
    that has been live longer than `MAX_GAME_DURATION_MINUTES` should be
    Final.  We use those bounds to keep MLB API calls minimal:

    1. Sleep until `last_first_pitch + MIN_GAME_DURATION_MINUTES` —
       no schedule fetches during this window.  No game can possibly
       be Final yet, so polling is wasted traffic.

    2. Then poll the schedule every `POST_LOCK_CHECK_INTERVAL` seconds
       until every game has final scores or sits in a NON_PLAYING
       status (Postponed, Cancelled, Suspended).  On all-final we
       clear the frozen cache and pre-warm tomorrow's schedule.

    Picks stay frozen; this loop never rebuilds the lineup.
    """
    from app.core.constants import NON_PLAYING_GAME_STATUSES
    from app.database import SessionLocal
    from app.models.slate import Slate, SlateGame
    from app.services.lineup_cache import lineup_cache
    from app.services.data_collection import fetch_schedule_for_date

    # ------------------------------------------------------------------
    # Phase 4a: Sleep until ANY game could plausibly be final.
    # No API calls happen here.
    # ------------------------------------------------------------------
    db = SessionLocal()
    try:
        last_first_pitch = _get_last_first_pitch_utc(db, today)
    finally:
        db.close()

    if last_first_pitch is not None:
        earliest_completion = last_first_pitch + timedelta(
            minutes=MIN_GAME_DURATION_MINUTES
        )
        now = datetime.now(timezone.utc)
        if now < earliest_completion:
            wait_min = (earliest_completion - now).total_seconds() / 60
            logger.info(
                "Post-lock monitor: sleeping until %s UTC (%.0f min) — "
                "earliest possible all-final time (last_first_pitch=%s + "
                "%d min)",
                earliest_completion.strftime("%H:%M"),
                wait_min,
                last_first_pitch.strftime("%H:%M"),
                MIN_GAME_DURATION_MINUTES,
            )
            await _sleep_until(earliest_completion)

    # ------------------------------------------------------------------
    # Phase 4b: Poll for completion at POST_LOCK_CHECK_INTERVAL cadence.
    # ------------------------------------------------------------------
    logger.info(
        "Post-lock monitor active — watching %s for completion (every %ds)",
        today, POST_LOCK_CHECK_INTERVAL,
    )

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
                    "Slate %s complete (%d games final) — purging frozen cache",
                    today, len(games),
                )
                _save_app_picks(db, today)
                # purge() (not clear()) so the Redis lineup:{today} +
                # lineup:meta:{today} keys are deleted alongside the in-memory
                # state.  Otherwise the 24h Redis TTL keeps yesterday's
                # payload alive: a dyno crash between slate-complete and
                # midnight would let restore_and_refreeze() re-freeze today's
                # finished slate (deploy_id matches, slate_date >= today),
                # and the user wouldn't see tomorrow's picks until midnight.
                lineup_cache.purge()

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
        try:
            await asyncio.wait_for(startup_done_event.wait(), timeout=300)
        except asyncio.TimeoutError:
            logger.critical(
                "Startup did not complete within 5 minutes — marking pipeline "
                "failed so /optimize returns 503 instead of spinning on 425. "
                "Investigate the most recent STARTUP STEP log line to find "
                "where init hung."
            )
            try:
                lineup_cache.mark_failed(
                    "Startup pipeline did not complete within 5 minutes — "
                    "see Railway logs for the most recent STARTUP STEP line."
                )
            except Exception:
                logger.exception("mark_failed() itself raised — continuing anyway")
            return
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
        # Phase 2: Sleep until T-65
        # ---------------------------------------------------------------
        # No external work happens here. All hydration (schedule, rosters,
        # stats, Statcast, vegas, weather, series) runs as a single block
        # inside run_full_pipeline at T-65 — one trigger, one failure
        # surface. See CLAUDE.md § "T-65 Sniper Architecture".
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
                except Exception as exc:
                    logger.exception(
                        "T-%d Phase 2b schedule refresh failed — marking "
                        "pipeline failed; /optimize will return 503.",
                        LOCK_MINUTES_BEFORE_PITCH,
                    )
                    lineup_cache.mark_failed(
                        f"Schedule refresh at T-{LOCK_MINUTES_BEFORE_PITCH} failed: {exc}"
                    )
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
                    lineup_cache.mark_failed(
                        f"No scheduled games found for {monitor_date} at "
                        f"T-{LOCK_MINUTES_BEFORE_PITCH}."
                    )
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
                        lineup_cache.mark_failed(
                            f"Lineup build at T-{LOCK_MINUTES_BEFORE_PITCH} "
                            "returned no candidates — slate data missing."
                        )
                        logger.error(
                            "T-%d lineup build returned nothing — no slate data "
                            "available. /optimize will return 503.",
                            LOCK_MINUTES_BEFORE_PITCH,
                        )
                except Exception as exc:
                    # Plain-text stderr dump so the traceback is visible in
                    # Railway's log UI — the JSON formatter embeds exc_info
                    # as an `exc` field that the UI doesn't surface, so
                    # failures previously appeared as just "PIPELINE FAILED"
                    # with no diagnostic content.
                    import sys as _sys
                    import traceback as _traceback
                    _sys.stderr.write(
                        f"\n=== T-{LOCK_MINUTES_BEFORE_PITCH} PIPELINE FAILED ===\n"
                    )
                    _sys.stderr.write(f"Exception type: {type(exc).__name__}\n")
                    _sys.stderr.write(f"Exception: {exc}\n")
                    _traceback.print_exc(file=_sys.stderr)
                    _sys.stderr.write("=== END PIPELINE FAILURE ===\n\n")
                    _sys.stderr.flush()
                    logger.exception(
                        "T-%d PIPELINE FAILED — see traceback below. "
                        "No fallback; /optimize will return 503.",
                        LOCK_MINUTES_BEFORE_PITCH,
                    )
                    lineup_cache.mark_failed(
                        f"T-{LOCK_MINUTES_BEFORE_PITCH} pipeline crashed: "
                        f"{type(exc).__name__}: {exc}"
                    )
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
        except Exception as exc:
            logger.critical(
                "T-65 monitor: unhandled exception escaped slate cycle — "
                "marking pipeline failed so /optimize returns 503 instead "
                "of spinning on 425. Restarting cycle in 60s.",
                exc_info=True,
            )
            try:
                lineup_cache.mark_failed(
                    f"Slate monitor crashed: {type(exc).__name__}: {exc}"
                )
            except Exception:
                logger.exception(
                    "mark_failed() itself raised — continuing anyway"
                )
            await asyncio.sleep(60)
