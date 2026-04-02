from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.pipeline import FetchResult, ScoreResult, PlayerSummary, PipelineResult
from app.services.pipeline import run_fetch, run_full_pipeline, run_score_slate

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
