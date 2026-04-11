from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db
from app.routers import players, slates, scoring, draft, calibration, pipeline, popularity, filter_strategy


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    import logging
    from datetime import date
    from pathlib import Path
    from app.database import SessionLocal
    from app.services.pipeline import run_full_pipeline
    from app.services.slate_monitor import targeted_slate_monitor
    from app.models.player import Player
    from app.seed import run_seed

    logger = logging.getLogger(__name__)

    Path(settings.database_url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
    init_db()

    # Seed database if empty
    db = SessionLocal()
    try:
        if db.query(Player).count() == 0:
            logger.info("Database empty, loading seed data...")
            run_seed(db)
    finally:
        db.close()

    # Purge all cache tiers on startup so every redeploy triggers a full
    # pipeline regeneration. Stale lineups (wrong starters, old scores)
    # must never survive a restart.
    from app.services.lineup_cache import lineup_cache
    lineup_cache.purge()

    # startup_done_event signals the T-65 monitor that the morning baseline
    # pipeline has finished and it is safe to compute lock timing.
    startup_done_event = asyncio.Event()

    # Run pipeline in background so health checks respond immediately
    async def _startup_pipeline():
        import traceback
        from datetime import timedelta
        from app.routers.filter_strategy import build_and_cache_lineups, _get_active_slate_date

        db = SessionLocal()
        try:
            # Stage 1: fetch schedule, rosters, stats, scores
            pipeline_ok = False
            try:
                result = await run_full_pipeline(db, date.today())
                logger.info("Startup pipeline complete: %s", result)
                pipeline_ok = True
            except Exception as exc:
                logger.error("Startup pipeline failed: %s\n%s", exc, traceback.format_exc())

            # If today has no games, also fetch the next day's slate
            active_date = _get_active_slate_date(db)
            if active_date != date.today():
                try:
                    result = await run_full_pipeline(db, active_date)
                    logger.info("Next-day pipeline complete (%s): %s", active_date, result)
                    pipeline_ok = True
                except Exception as exc:
                    logger.error("Next-day pipeline failed: %s\n%s", exc, traceback.format_exc())

            # Stage 2: morning baseline cache (NOT frozen — T-65 monitor freezes later)
            if pipeline_ok:
                try:
                    cached = await build_and_cache_lineups(db)
                    if cached:
                        logger.info("Morning baseline ready — T-65 monitor will freeze at lock time")
                    else:
                        logger.warning("Morning baseline empty after startup (no slate data?)")
                except Exception as exc:
                    logger.error("Morning baseline failed: %s\n%s", exc, traceback.format_exc())

            # Publish schedule immediately so the UI shows "Picks Available
            # at HH:MM" instead of "Preparing Today's Picks" while waiting
            # for the T-65 monitor to wake up.  Runs even when pipeline_ok is
            # False — the slate/games may already exist from a prior run.
            try:
                from app.services.slate_monitor import _get_first_pitch_utc

                first_pitch = _get_first_pitch_utc(db, active_date)
                if first_pitch is not None:
                    lineup_cache.set_schedule(first_pitch)
                    logger.info("Schedule published: first pitch %s UTC", first_pitch.strftime("%H:%M"))
            except Exception as exc:
                logger.warning("Could not publish schedule at startup: %s", exc)
        finally:
            db.close()
            # Always signal the monitor so it can compute lock timing even if
            # the pipeline partially failed.
            startup_done_event.set()

    startup_task = asyncio.create_task(_startup_pipeline())
    monitor_task = asyncio.create_task(targeted_slate_monitor(startup_done_event))

    yield

    startup_task.cancel()
    monitor_task.cancel()
    for task in [startup_task, monitor_task]:
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Baseball DFS Engine",
    description="Pre-draft scoring, ranking, and lineup optimization for baseball DFS",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(players.router, prefix="/api/players", tags=["players"])
app.include_router(slates.router, prefix="/api/slates", tags=["slates"])
app.include_router(scoring.router, prefix="/api/score", tags=["scoring"])
app.include_router(draft.router, prefix="/api/draft", tags=["draft"])
app.include_router(calibration.router, prefix="/api/calibration", tags=["calibration"])
app.include_router(pipeline.router, prefix="/api/pipeline", tags=["pipeline"])
app.include_router(popularity.router, prefix="/api/popularity", tags=["popularity"])
app.include_router(filter_strategy.router, prefix="/api/filter-strategy", tags=["filter-strategy"])


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}
