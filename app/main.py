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
            "stream": "ext://sys.stdout",
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
    from app.services.slate_monitor import targeted_slate_monitor
    from app.seed import run_seed

    logger = logging.getLogger(__name__)

    # Startup Diagnostic: which env vars are present? (names only, never values — secrets).
    # This is the definitive source of truth for debugging Railway env-var injection.
    import os as _os
    _env_presence = {
        "BO_REDIS_URL": bool(_os.environ.get("BO_REDIS_URL")),
        "REDIS_URL": bool(_os.environ.get("REDIS_URL")),
        "REDIS_PRIVATE_URL": bool(_os.environ.get("REDIS_PRIVATE_URL")),
        "BO_DATABASE_URL": bool(_os.environ.get("BO_DATABASE_URL")),
        "DATABASE_URL": bool(_os.environ.get("DATABASE_URL")),
        "BO_CURRENT_SEASON": bool(_os.environ.get("BO_CURRENT_SEASON")),
        "BO_ODDS_API_KEY": bool(_os.environ.get("BO_ODDS_API_KEY")),
        "PORT": _os.environ.get("PORT", "unset"),
    }
    logger.info("STARTUP ENV PRESENCE: %s", _env_presence)
    logger.info(
        "STARTUP RESOLVED: settings.redis_url=%s, settings.database_url_scheme=%s",
        "SET" if settings.redis_url else "UNSET",
        settings.database_url.split("://", 1)[0] if "://" in settings.database_url else "unknown",
    )

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

    # Fast pre-yield validation: URL-format checks only (no I/O).
    # Railway's container runtime requires the process to bind $PORT within a
    # short grace window (empirically <10s). FastAPI's lifespan blocks uvicorn
    # from binding until yield is reached, so all I/O-heavy startup work
    # (alembic migrations, Redis ping, seed, cache restore) is moved to a
    # background task that runs AFTER yield. This lets /api/health respond
    # immediately for the Railway healthcheck while readiness for the pipeline
    # endpoints stays gated on startup_done_event.
    #
    # Failure semantics are preserved: if any step in _startup_init fails, the
    # event stays unset, the T-65 monitor never fires, and
    # /api/filter-strategy/optimize returns 503. No silent fallbacks — the
    # failure is logged at CRITICAL and surfaced on the pipeline endpoints.
    if not settings.redis_url:
        raise RuntimeError(
            "CRITICAL: BO_REDIS_URL is not set. Redis is required for the cache "
            "layer (frozen T-65 picks, multi-replica coordination). No DB-only "
            "fallback. Set BO_REDIS_URL before starting the app."
        )

    logger.info("MLB season: BO_CURRENT_SEASON=%d", settings.current_season)
    if not settings.odds_api_key:
        logger.critical(
            "BO_ODDS_API_KEY not configured. Vegas lines are REQUIRED for optimal lineup generation. "
            "T-65 pipeline will crash if Vegas API cannot be called. Set BO_ODDS_API_KEY environment variable."
        )
    else:
        logger.info("BO_ODDS_API_KEY configured — Vegas API enrichment enabled")

    startup_done_event = asyncio.Event()

    def _sync_startup_init() -> None:
        """Blocking startup work. Runs in a worker thread via asyncio.to_thread."""
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from app.services.lineup_cache import lineup_cache
        from app.services.slate_monitor import _get_first_pitch_utc, LOCK_MINUTES_BEFORE_PITCH
        from app.models.slate import Slate as _Slate
        import redis as redis_lib
        import time as _time

        logger.info("STARTUP STEP: calling init_db() (alembic migrations)")
        init_db()
        logger.info("STARTUP STEP: init_db() completed successfully")

        logger.info("STARTUP STEP: validating Redis connectivity")
        _redis_error: Exception | None = None
        for _attempt in range(1, 6):
            try:
                client = redis_lib.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=10,
                )
                client.ping()
                logger.info("Redis connectivity verified at startup (attempt %d)", _attempt)
                _redis_error = None
                break
            except Exception as e:
                _redis_error = e
                if _attempt < 5:
                    logger.warning(
                        "Redis ping failed (attempt %d/5): %s — retrying in 2s", _attempt, e
                    )
                    _time.sleep(2)
        if _redis_error is not None:
            raise RuntimeError(
                f"CRITICAL: Redis unreachable after 5 startup attempts. "
                f"Redis is required for cache layer — no fallback. Last error: {_redis_error}"
            )

        logger.info("STARTUP STEP: running seed")
        with SessionLocal() as db:
            run_seed(db)

        logger.info("STARTUP STEP: initializing lineup cache")
        _restored = False
        with SessionLocal() as _check_db:
            _today_slate = _check_db.query(_Slate).filter_by(date=date.today()).first()
            if _today_slate:
                _first_pitch = _get_first_pitch_utc(_check_db, date.today())
                if _first_pitch:
                    _lock_time = _first_pitch - _td(minutes=LOCK_MINUTES_BEFORE_PITCH)
                    if _dt.now(_tz.utc) >= _lock_time:
                        _restored = lineup_cache.restore_and_refreeze(_first_pitch)
                        if _restored:
                            logger.info(
                                "Post-T-65 restart: restored frozen picks. "
                                "Monitor will skip pipeline and proceed to post-lock monitoring."
                            )
                        else:
                            logger.warning(
                                "Post-T-65 restart: no cached picks to restore. "
                                "Monitor will attempt pipeline regeneration (may fail if games are Live)."
                            )
        if not _restored:
            lineup_cache.purge()

    async def _startup_init():
        try:
            await asyncio.to_thread(_sync_startup_init)
        except Exception:
            logger.critical(
                "STARTUP FAILED: background init raised. /api/filter-strategy/optimize "
                "will stay in 503 until the underlying issue is fixed and the app is "
                "restarted. No fallback.",
                exc_info=True,
            )
            return
        logger.info(
            "Startup complete. "
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
    title="Ben Oracle",
    description="MLB lineup draft optimizer",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # No cookies / Authorization header are used by this API, so credentials
    # are off. This also makes `allow_origins=["*"]` valid per the CORS spec
    # (credentialed requests with wildcard origins are rejected by browsers).
    allow_credentials=False,
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
