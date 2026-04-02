from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.pipeline import run_fetch, run_full_pipeline, run_score_slate

router = APIRouter()


@router.post("/fetch/{game_date}")
async def fetch_data(game_date: date, db: Session = Depends(get_db)):
    """Fetch MLB schedule for a date and create slate."""
    return await run_fetch(db, game_date)


@router.post("/score/{game_date}")
def score_data(game_date: date, db: Session = Depends(get_db)):
    """Score all players for a slate (no API fetch, uses existing DB data)."""
    results = run_score_slate(db, game_date)
    return {
        "date": game_date.isoformat(),
        "scored": len(results),
        "top_5": [
            {"name": r.player_name, "score": r.total_score, "est_rs": r.estimated_rs_mid}
            for r in results[:5]
        ],
    }


@router.post("/run/{game_date}")
async def run_pipeline(game_date: date, db: Session = Depends(get_db)):
    """Full pipeline: fetch schedule → fetch stats → score → rank."""
    return await run_full_pipeline(db, game_date)
