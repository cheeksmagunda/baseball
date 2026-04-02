from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.calibration import WeightsOut, WeightsIn
from app.core.weights import get_current_weights, save_weights, ScoringWeights

router = APIRouter()


@router.get("/weights", response_model=WeightsOut)
def get_weights(db: Session = Depends(get_db)):
    weights = get_current_weights(db)
    return WeightsOut(
        pitcher=asdict(weights.pitcher),
        batter=asdict(weights.batter),
    )


@router.put("/weights", response_model=WeightsOut)
def update_weights(body: WeightsIn, db: Session = Depends(get_db)):
    weights = get_current_weights(db)
    if body.pitcher:
        for k, v in body.pitcher.items():
            if hasattr(weights.pitcher, k):
                setattr(weights.pitcher, k, v)
    if body.batter:
        for k, v in body.batter.items():
            if hasattr(weights.batter, k):
                setattr(weights.batter, k, v)

    save_weights(db, weights, notes=body.notes)
    return WeightsOut(
        pitcher=asdict(weights.pitcher),
        batter=asdict(weights.batter),
    )
