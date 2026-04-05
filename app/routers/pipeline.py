from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.pipeline import (
    FetchResult, ScoreResult, PlayerSummary, PipelineResult,
    FilterStrategyPipelineResult,
)
from app.services.pipeline import run_fetch, run_full_pipeline, run_score_slate, run_filter_strategy_from_slate

router = APIRouter()


@router.post("/fetch/{game_date}", response_model=FetchResult)
async def fetch_data(game_date: date, db: Session = Depends(get_db)):
    """Fetch MLB schedule for a date and create slate."""
    return await run_fetch(db, game_date)


@router.post("/score/{game_date}", response_model=ScoreResult)
def score_data(game_date: date, db: Session = Depends(get_db)):
    """Score all players for a slate (no API fetch, uses existing DB data)."""
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
    """Full pipeline: fetch schedule → fetch stats → score → rank."""
    return await run_full_pipeline(db, game_date)


@router.post("/filter-strategy/{game_date}", response_model=FilterStrategyPipelineResult)
def run_filter_strategy_pipeline(game_date: date, db: Session = Depends(get_db)):
    """
    Run the "Filter, Not Forecast" strategy on an existing scored slate.

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
    result = run_filter_strategy_from_slate(db, game_date)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result
