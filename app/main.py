from contextlib import asynccontextmanager
import logging
import logging.config

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db
from app.routers import players, slates, scoring, draft, calibration, pipeline, popularity, filter_strategy


# Centralized logging configuration
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "default": {
            "level": settings.log_level,
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "loggers": {
        "": {  # root logger
            "handlers": ["default"],
            "level": settings.log_level,
            "propagate": True,
        },
        "sqlalchemy": {
            "handlers": ["default"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

logging.config.dictConfig(LOGGING_CONFIG)


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

    # Startup Validation: Database URL
    try:
        db_url = settings.database_url
        if db_url.startswith("sqlite:///"):
            db_path = db_url.replace("sqlite:///", "")
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            if not Path(db_path).parent.exists():
                raise RuntimeError(f"Cannot create database directory: {db_path}")
            logger.info("SQLite database directory validated: %s", db_path)
        elif db_url.startswith("postgresql://") or db_url.startswith("postgresql+psycopg2://"):
            # Validate Postgres connection string format
            if not ("@" in db_url and ":" in db_url):
                raise RuntimeError(f"Invalid Postgres URL format: {db_url}")
            logger.info("Postgres database URL format validated")
        else:
            raise RuntimeError(f"Unsupported database URL scheme: {db_url}")
    except Exception as e:
        raise RuntimeError(f"Database URL validation failed at startup: {e}")

    init_db()

    # Startup Validation: Redis (if configured)
    if settings.redis_url:
        try:
            import redis as redis_lib
            client = redis_lib.from_url(settings.redis_url, decode_responses=True)
            client.ping()
            logger.info("Redis connectivity verified at startup")
        except Exception as e:
            raise RuntimeError(
                f"CRITICAL: Redis configured but unreachable at startup. "
                f"Redis is required for cache layer — no fallback to SQLite. "
                f"Restore Redis dyno or remove DFS_REDIS_URL from config. Error: {e}"
            )

    # Startup Validation: Odds API Key
    if not settings.odds_api_key:
        logger.critical(
            "DFS_ODDS_API_KEY not configured. Vegas lines are REQUIRED for optimal lineup generation. "
            "T-65 pipeline will crash if Vegas API cannot be called. Set DFS_ODDS_API_KEY environment variable."
        )
    else:
        logger.info("DFS_ODDS_API_KEY configured — Vegas API enrichment enabled")

    # Seed database if empty
    with SessionLocal() as db:
        if db.query(Player).count() == 0:
            logger.info("Database empty, loading seed data...")
            run_seed(db)

    # Cache initialization (FAIL-LOUD principle):
    # Always purge cache on startup. Do NOT restore frozen picks from previous runs.
    # If T-65 has already passed, the T-65 monitor will detect this and immediately
    # regenerate picks from scratch with fresh live data. This ensures every pick
    # served to users is built from complete, current data — never from cached
    # or fallback sources.
    #
    # If app restarts after T-65 and any dependency (MLB API, Odds API, DB) is
    # unavailable, the monitor will crash loudly (no fallback to stale data).
    from app.services.lineup_cache import lineup_cache

    lineup_cache.purge()

    # startup_done_event signals the T-65 monitor that initialization is complete.
    startup_done_event = asyncio.Event()

    # Minimal startup: zero API calls, zero pipeline work, zero optimization.
    # The T-65 monitor is the SOLE trigger for the full pipeline (fetch→score→optimize).
    # Picks are locked at T-65 and served from cache until slate completion.
    # See CLAUDE.md § "T-65 Sniper Architecture" for detailed timing model.
    async def _startup_init():
        logger.info(
            "Startup complete. Cache purged. "
            "T-65 monitor will fetch fresh data and generate lineups at T-65 lock time. "
            "No other pipeline work allowed during active slate."
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
