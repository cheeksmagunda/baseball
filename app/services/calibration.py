"""
Calibration service: compare predicted vs actual RS,
compute metrics, and optionally adjust weights.
"""

from datetime import date

import numpy as np
from sqlalchemy.orm import Session

from app.models.slate import Slate, SlatePlayer
from app.models.calibration import CalibrationResult
from app.core.utils import get_latest_player_score
from app.core.weights import get_current_weights, save_weights


def calibrate_slate(db: Session, game_date: date) -> dict:
    """
    Compare predicted RS (from PlayerScore) vs actual RS (from SlatePlayer)
    for a completed slate. Returns calibration metrics.
    """
    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        return {"error": "No slate found"}

    # Get all slate players with both predicted scores and actual RS
    pairs = []
    slate_players = db.query(SlatePlayer).filter_by(slate_id=slate.id).all()

    for sp in slate_players:
        if sp.real_score is None:
            continue

        # Get the most recent prediction for this slate player
        ps = get_latest_player_score(db, sp.id)
        if not ps:
            continue

        pairs.append({
            "player_score": ps.total_score,
            "actual_rs": sp.real_score,
            "is_hv": sp.is_highest_value,
        })

    if not pairs:
        return {"error": "No predictions to compare"}

    scores = np.array([p["player_score"] for p in pairs])
    actual = np.array([p["actual_rs"] for p in pairs])

    # Spearman rank correlation (score ranking vs actual RS ranking)
    correlation = None
    if len(pairs) > 2:
        from scipy.stats import spearmanr
        try:
            corr, _ = spearmanr(scores, actual)
            correlation = float(corr) if not np.isnan(corr) else None
        except Exception:
            # scipy not available — compute manually
            pass

    # Top quintile hit rate: of our top-20% scored players, how many were actually HV?
    top_quintile_hit_rate = None
    if len(pairs) >= 5:
        sorted_by_score = sorted(pairs, key=lambda p: p["player_score"], reverse=True)
        top_n = max(1, len(sorted_by_score) // 5)
        top_players = sorted_by_score[:top_n]
        hv_hits = sum(1 for p in top_players if p["is_hv"])
        top_quintile_hit_rate = hv_hits / top_n

    # Store result
    result = CalibrationResult(
        slate_id=slate.id,
        mean_absolute_error=0.0,  # deprecated — score and RS are different scales
        correlation=round(correlation, 3) if correlation is not None else None,
        top_quintile_hit_rate=round(top_quintile_hit_rate, 3) if top_quintile_hit_rate else None,
    )
    db.add(result)

    # Mark slate as calibrated
    slate.status = "calibrated"
    db.commit()

    return {
        "date": game_date.isoformat(),
        "pairs_evaluated": len(pairs),
        "correlation": result.correlation,
        "top_quintile_hit_rate": result.top_quintile_hit_rate,
    }
