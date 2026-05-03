from contextlib import asynccontextmanager
import logging
import logging.config
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.core.logging_config import JsonFormatter, request_id_var
from app.database import init_db
from app.routers import players, slates, scoring, pipeline, filter_strategy
from app.services import app_state as _app_state


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": JsonFormatter,
        },
    },
    "handlers": {
        "default": {
            "level": settings.log_level,
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json",
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


# ---------------------------------------------------------------------------
# Request-ID middleware — injects a short correlation ID into every log line
# ---------------------------------------------------------------------------

class _RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["x-request-id"] = rid
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    import logging
    from datetime import date
    from pathlib import Path
    from app.database import SessionLocal
    from app.services.slate_monitor import targeted_slate_monitor

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

    # Fast pre-yield validation: URL-format checks only (no I/O).
    # Railway's container runtime requires the process to bind $PORT within a
    # short grace window (empirically <10s). FastAPI's lifespan blocks uvicorn
    # from binding until yield is reached, so all I/O-heavy startup work
    # (schema create_all, Redis ping, cache restore) is moved to a
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
        raise RuntimeError(
            "CRITICAL: BO_ODDS_API_KEY is not set. Vegas lines (moneyline + O/U totals) are "
            "required inputs to pitcher and batter env scoring — the T-65 pipeline cannot run "
            "without them. Set BO_ODDS_API_KEY to your The Odds API key before starting the app."
        )
    logger.info("BO_ODDS_API_KEY configured — Vegas API enrichment enabled")

    # Use the module-level event so the health endpoint can read it
    startup_done_event = _app_state.startup_done_event

    def _sync_startup_init() -> None:
        """Blocking startup work. Runs in a worker thread via asyncio.to_thread."""
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from app.services.lineup_cache import lineup_cache
        from app.services.slate_monitor import _get_first_pitch_utc
        from app.models.slate import Slate as _Slate
        import redis as redis_lib
        import time as _time

        _t0 = _time.monotonic()

        logger.info("STARTUP STEP 1/3: create_all() — building schema from models")
        init_db()
        logger.info(
            "STARTUP STEP 1/3 complete (%.2fs elapsed)", _time.monotonic() - _t0
        )

        logger.info("STARTUP STEP 2/3: validating Redis connectivity")
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
        logger.info(
            "STARTUP STEP 2/3 complete (%.2fs elapsed)", _time.monotonic() - _t0
        )

        logger.info("STARTUP STEP 3/3: initializing lineup cache")
        _restored = False
        with SessionLocal() as _check_db:
            _today_slate = _check_db.query(_Slate).filter_by(date=date.today()).first()
            if _today_slate:
                _first_pitch = _get_first_pitch_utc(_check_db, date.today())
                if _first_pitch:
                    # Lock_time is the boundary at which the monitor freezes
                    # picks. Once it has passed, a cache row may exist for
                    # today's slate. Try to restore — restore_and_refreeze
                    # checks the deploy_id internally:
                    #   * same deploy_id (transient crash) → restore unchanged
                    #   * different deploy_id (code push / manual redeploy) → False
                    #     → purge() below busts cache → monitor runs fresh on
                    #       remaining games.
                    # Pre-lock: no cache exists yet, restore returns False,
                    # purge() is a no-op.
                    _lock_time = _first_pitch - _td(minutes=65)
                    if _dt.now(_tz.utc) >= _lock_time:
                        _restored = lineup_cache.restore_and_refreeze(_first_pitch)
                        if _restored:
                            logger.info(
                                "Restart after lock: restored frozen picks (same "
                                "deploy_id). Monitor will skip pipeline and "
                                "proceed to post-lock monitoring."
                            )
                        else:
                            logger.warning(
                                "Restart after lock: no cached picks restored "
                                "(no cache row OR new deploy_id). Monitor will "
                                "run pipeline on remaining games."
                            )
        if not _restored:
            lineup_cache.purge()
        logger.info(
            "STARTUP STEP 3/3 complete (%.2fs elapsed)", _time.monotonic() - _t0
        )

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
        startup_done_event.set()
        logger.info(
            "Startup complete. "
            "T-65 monitor will fetch fresh data and generate lineups at T-65 lock time. "
            "No other pipeline work allowed during active slate."
        )

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

app.add_middleware(_RequestIDMiddleware)
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
app.include_router(pipeline.router, prefix="/api/pipeline", tags=["pipeline"])
app.include_router(filter_strategy.router, prefix="/api/filter-strategy", tags=["filter-strategy"])


@app.get("/api/health")
async def health():
    """Deep health check: startup, Redis, and DB connectivity.

    Returns HTTP 200 with status="ok" when all dependencies are healthy.
    Returns HTTP 503 with status="degraded" when any dependency is down so
    Railway's health probe can trigger a restart on unrecoverable failures.
    """
    import asyncio as _asyncio
    import sqlalchemy as _sa
    from fastapi.responses import JSONResponse as _JSONResponse

    checks: dict[str, str] = {}

    # 1. Startup completion
    checks["startup"] = "ok" if _app_state.startup_done_event.is_set() else "starting"

    # 2. Redis connectivity (synchronous ping via thread pool)
    async def _check_redis() -> str:
        if not settings.redis_url:
            return "unconfigured"
        try:
            import redis as _redis
            def _ping():
                c = _redis.from_url(
                    settings.redis_url,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                c.ping()
            await _asyncio.to_thread(_ping)
            return "ok"
        except Exception as exc:
            return f"error: {exc}"

    # 3. DB connectivity
    async def _check_db() -> str:
        try:
            from app.database import SessionLocal
            def _select1():
                with SessionLocal() as db:
                    db.execute(_sa.text("SELECT 1"))
            await _asyncio.to_thread(_select1)
            return "ok"
        except Exception as exc:
            return f"error: {exc}"

    checks["redis"], checks["db"] = await _asyncio.gather(
        _check_redis(), _check_db()
    )

    ok = all(v == "ok" for v in checks.values())
    return _JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ok" if ok else "degraded",
            "version": "0.1.0",
            "checks": checks,
        },
    )
