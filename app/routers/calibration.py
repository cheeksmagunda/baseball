from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.calibration import CalibrationResult
from app.models.slate import Slate
from app.schemas.calibration import CalibrationResultOut, WeightsOut, WeightsIn
from app.core.weights import get_current_weights, save_weights, ScoringWeights
from app.services.calibration import calibrate_slate

router = APIRouter()


@router.post("/{slate_date}")
def run_calibration(slate_date: date, db: Session = Depends(get_db)):
    """Compare predicted vs actual RS for a completed slate."""
    return calibrate_slate(db, slate_date)


@router.get("/history", response_model=list[CalibrationResultOut])
def calibration_history(db: Session = Depends(get_db)):
    results = (
        db.query(CalibrationResult)
        .order_by(CalibrationResult.created_at.desc())
        .limit(50)
        .all()
    )
    out = []
    for r in results:
        slate = db.query(Slate).get(r.slate_id)
        out.append(CalibrationResultOut(
            slate_date=slate.date if slate else date.min,
            mean_absolute_error=r.mean_absolute_error,
            correlation=r.correlation,
            top_quintile_hit_rate=r.top_quintile_hit_rate,
            notes=r.notes,
        ))
    return out


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
