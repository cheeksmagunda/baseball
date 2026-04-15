import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.utils import is_pipeline_callable_now
from app.schemas.pipeline import (
    FetchResult, ScoreResult, PlayerSummary, PipelineResult,
    FilterStrategyPipelineResult,
)
from app.services.pipeline import run_fetch, run_full_pipeline, run_score_slate, run_filter_strategy_from_slate

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/fetch/{game_date}", response_model=FetchResult)
async def fetch_data(game_date: date, db: Session = Depends(get_db)):
    """
    Fetch MLB schedule for a date and create slate.

    WARNING: This endpoint is for manual/testing use only. During active slates
    (games in progress), all API calls are locked and routed through the T-65
    slate monitor exclusively. This endpoint can only be called after all games
    finish and a new slate day begins.
    """
    can_call, reason = is_pipeline_callable_now(db)
    if not can_call:
        raise HTTPException(
            status_code=423,
            detail=reason,
        )
    return await run_fetch(db, game_date)


@router.post("/score/{game_date}", response_model=ScoreResult)
def score_data(game_date: date, db: Session = Depends(get_db)):
    """
    Score all players for a slate (no API fetch, uses existing DB data).

    WARNING: This endpoint is for manual/testing use only. During active slates,
    all work is locked and routed through the T-65 slate monitor exclusively.
    """
    can_call, reason = is_pipeline_callable_now(db)
    if not can_call:
        raise HTTPException(
            status_code=423,
            detail=reason,
        )
    results = run_score_slate(db, game_date)
    return ScoreResult(
        date=game_date.isoformat(),
        scored=len(results),
        top_5=[
            PlayerSummary(name=r.player_name, score=r.total_score)
            for r in results[:5]
        ],
    )


@router.post("/run/{game_date}", response_model=PipelineResult)
async def run_pipeline(game_date: date, db: Session = Depends(get_db)):
    """
    Full pipeline: fetch schedule → fetch stats → score → rank.

    WARNING: This endpoint is for manual/testing use only. During active slates,
    all pipeline work is locked and routed through the T-65 slate monitor
    exclusively. This endpoint can only be called after all games finish.
    """
    can_call, reason = is_pipeline_callable_now(db)
    if not can_call:
        raise HTTPException(
            status_code=423,
            detail=reason,
        )
    return await run_full_pipeline(db, game_date)


@router.post("/filter-strategy/{game_date}", response_model=FilterStrategyPipelineResult)
def run_filter_strategy_pipeline(game_date: date, db: Session = Depends(get_db)):
    """
    Run the "Filter, Not Forecast" strategy on an existing scored slate.

    WARNING: This endpoint is for manual/testing use only. During active slates,
    all optimization is locked and routed through the T-65 slate monitor
    exclusively. This endpoint can only be called after all games finish.

    Prerequisites: slate must exist with players and (ideally) game
    environment data populated. Run /pipeline/run/{date} first to
    create the slate and score players.

    This implements the full 5-filter pipeline from the Master Strategy Doc:
    1. Slate classification (tiny/pitcher_day/hitter_day/standard)
    2. Environmental advantage scoring
    3. Ownership leverage adjustment
    4. Boost-environment gating
    5. Smart slot assignment with composition enforcement
    """
    can_call, reason = is_pipeline_callable_now(db)
    if not can_call:
        raise HTTPException(
            status_code=423,
            detail=reason,
        )
    result = run_filter_strategy_from_slate(db, game_date)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result
