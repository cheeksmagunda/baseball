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

    # Run pipeline in background so health checks respond immediately
    async def _startup_pipeline():
        import traceback
        from app.routers.filter_strategy import build_and_cache_lineups

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

            # Stage 2: pre-compute and cache lineups (only if pipeline succeeded)
            if pipeline_ok:
                try:
                    cached = await build_and_cache_lineups(db)
                    if cached:
                        logger.info("Lineup cache ready — frontend requests will be instant")
                    else:
                        logger.warning("Lineup cache empty after startup (no slate data?)")
                except Exception as exc:
                    logger.error("Lineup cache warm failed: %s\n%s", exc, traceback.format_exc())
        finally:
            db.close()

    task = asyncio.create_task(_startup_pipeline())

    yield

    task.cancel()
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
