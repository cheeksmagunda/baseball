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
    with SessionLocal() as db:
        if db.query(Player).count() == 0:
            logger.info("Database empty, loading seed data...")
            run_seed(db)

    # Restart-during-live-slate guard (V8.1):
    # If today's picks are already frozen in the DB/Redis (i.e., a Railway
    # dyno restarted after T-65), restore and re-freeze them instead of
    # wiping and regenerating from a reduced candidate pool.  Otherwise,
    # purge all cache tiers so the new startup always gets fresh data.
    #
    # This prevents the "dirty mid-run data" bug where a restart after T-65
    # (but before game completion) would regenerate picks from a reduced pool
    # (started/final games excluded), producing different picks than the ones
    # that were locked at T-65, violating the "zero work outside T-65" rule.
    from datetime import datetime, timedelta, timezone
    from app.services.lineup_cache import lineup_cache
    from app.services.slate_monitor import _get_first_pitch_utc, LOCK_MINUTES_BEFORE_PITCH

    _restored_frozen = False
    try:
        with SessionLocal() as _db_check:
            from app.routers.filter_strategy import _get_active_slate_date
            _active = _get_active_slate_date(_db_check)
            if _active == date.today():
                _fp = _get_first_pitch_utc(_db_check, date.today())
                if _fp is not None:
                    _lock = _fp - timedelta(minutes=LOCK_MINUTES_BEFORE_PITCH)
                    if datetime.now(timezone.utc) >= _lock:
                        # T-65 has already passed — attempt to restore frozen picks
                        _restored_frozen = lineup_cache.restore_and_refreeze(_fp)
    except Exception as _exc:
        logger.warning("Startup freeze-restore check failed (will purge): %s", _exc)

    if not _restored_frozen:
        lineup_cache.purge()

    # startup_done_event signals the T-65 monitor that initialization is complete.
    startup_done_event = asyncio.Event()

    # Minimal startup: zero API calls, zero pipeline work, zero optimization.
    # The T-65 monitor is the SOLE trigger for the full pipeline (fetch→score→optimize).
    # Picks are locked at T-65 and served from cache until slate completion.
    # See CLAUDE.md § "T-65 Sniper Architecture" for detailed timing model.
    async def _startup_init():
        import traceback
        logger.info(
            "Startup: frozen picks restored=%s. "
            "T-65 monitor will fetch fresh data and generate lineups at T-65 lock time. "
            "No other pipeline work allowed during active slate.",
            _restored_frozen,
        )
        startup_done_event.set()

    startup_task = asyncio.create_task(_startup_init())
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
