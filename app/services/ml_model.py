"""
Gradient Boosting model for predicting High-Value probability.

Augments the static CONDITION_MATRIX with a data-driven signal trained on
historical_players.csv. The model learns non-linear interactions between
card_boost, drafts, and position that the 25-cell matrix can't capture.

Usage:
    # At startup or after new data arrives:
    from app.services.ml_model import train_hv_model, predict_hv_probability

    train_hv_model()  # trains and saves to data/hv_model.joblib

    # At runtime (inside condition_classifier):
    prob = predict_hv_probability(card_boost=3.0, drafts=5, is_pitcher=False)
"""

import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MODEL_PATH = DATA_DIR / "hv_model.joblib"
CSV_PATH = DATA_DIR / "historical_players.csv"

# High-value threshold: total_value >= 15 means the player was a winning pick
HV_THRESHOLD = 15.0

# Blend weight: how much influence the ML model has vs the static matrix.
# 0.0 = pure matrix (status quo), 1.0 = pure ML.
# Start conservative — the matrix is proven over 19 dates.
ML_BLEND_WEIGHT = 0.30

# Cache the loaded model in-process so we don't re-read from disk every call.
_cached_model = None
_model_loaded = False


def train_hv_model() -> dict:
    """Train a GradientBoosting classifier on historical_players.csv.

    Features: card_boost, drafts, is_pitcher, drafts_x_boost (interaction)
    Target: total_value >= HV_THRESHOLD (binary)

    Returns a dict with training metrics.
    """
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import cross_val_score
    import joblib

    if not CSV_PATH.exists():
        raise FileNotFoundError(f"Training data not found: {CSV_PATH}")

    df = pd.read_csv(CSV_PATH)

    # Clean data
    df["card_boost"] = df["card_boost"].fillna(0.0)
    df["drafts"] = df["drafts"].fillna(df["drafts"].median())
    df = df.dropna(subset=["total_value"])

    # Feature engineering
    df["is_pitcher"] = df["position"].isin({"P", "SP", "RP"}).astype(int)
    df["log_drafts"] = np.log1p(df["drafts"])
    df["boost_x_log_drafts"] = df["card_boost"] * df["log_drafts"]
    from app.core.constants import GHOST_DRAFT_THRESHOLD
    df["is_ghost"] = (df["drafts"] < GHOST_DRAFT_THRESHOLD).astype(int)
    df["ghost_x_boost"] = df["is_ghost"] * df["card_boost"]

    features = ["card_boost", "log_drafts", "is_pitcher", "boost_x_log_drafts",
                "is_ghost", "ghost_x_boost"]
    X = df[features].values
    y = (df["total_value"] >= HV_THRESHOLD).astype(int).values

    # Train with modest hyperparameters — 488 rows, prevent overfitting
    model = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=3,
        learning_rate=0.1,
        min_samples_leaf=10,
        subsample=0.8,
        random_state=42,
    )

    # Cross-validate before final fit
    cv_scores = cross_val_score(model, X, y, cv=5, scoring="roc_auc")
    logger.info(
        "HV model 5-fold CV AUC: %.3f +/- %.3f",
        cv_scores.mean(), cv_scores.std(),
    )

    # Final fit on all data
    model.fit(X, y)

    # Feature importances for logging
    importances = dict(zip(features, model.feature_importances_))
    logger.info("HV model feature importances: %s", importances)

    # Save
    os.makedirs(DATA_DIR, exist_ok=True)
    joblib.dump({"model": model, "features": features}, MODEL_PATH)
    logger.info("HV model saved to %s", MODEL_PATH)

    # Reset cache so next call loads the new model
    global _cached_model, _model_loaded
    _cached_model = None
    _model_loaded = False

    return {
        "cv_auc_mean": round(cv_scores.mean(), 4),
        "cv_auc_std": round(cv_scores.std(), 4),
        "feature_importances": {k: round(v, 4) for k, v in importances.items()},
        "training_samples": len(y),
        "positive_rate": round(y.mean(), 4),
    }


def _load_model():
    """Load the trained model from disk (cached in-process)."""
    global _cached_model, _model_loaded

    if _model_loaded:
        return _cached_model

    _model_loaded = True

    if not MODEL_PATH.exists():
        logger.debug("No HV model found at %s — ML predictions disabled", MODEL_PATH)
        _cached_model = None
        return None

    try:
        import joblib
        data = joblib.load(MODEL_PATH)
        _cached_model = data
        logger.info("HV model loaded from %s", MODEL_PATH)
        return data
    except Exception as exc:
        logger.warning("Failed to load HV model: %s — ML predictions disabled", exc)
        _cached_model = None
        return None


def predict_hv_probability(
    card_boost: float,
    drafts: int | None,
    is_pitcher: bool,
) -> float | None:
    """Predict P(total_value >= 15) for a single player.

    Returns None if the model isn't available (graceful degradation).
    """
    data = _load_model()
    if data is None:
        return None

    model = data["model"]

    _drafts = float(drafts) if drafts is not None else 500.0  # neutral default
    _boost = float(card_boost)
    _pitcher = 1.0 if is_pitcher else 0.0
    _log_drafts = np.log1p(_drafts)
    _boost_x_log_drafts = _boost * _log_drafts
    from app.core.constants import GHOST_DRAFT_THRESHOLD
    _is_ghost = 1.0 if _drafts < GHOST_DRAFT_THRESHOLD else 0.0
    _ghost_x_boost = _is_ghost * _boost

    X = np.array([[_boost, _log_drafts, _pitcher, _boost_x_log_drafts,
                    _is_ghost, _ghost_x_boost]])

    prob = model.predict_proba(X)[0, 1]
    return float(prob)


def get_blended_hv_rate(
    matrix_rate: float,
    card_boost: float,
    drafts: int | None,
    is_pitcher: bool,
) -> float:
    """Blend the static matrix HV rate with the ML prediction.

    If the ML model isn't loaded, returns the matrix rate unchanged.

    V3.0: The ML model can now contribute signal for all conditions, including
    formerly dead-capital ones.  The Bayesian floor in condition_classifier
    ensures matrix_rate is never 0.0, so the blend always operates.
    The ML model's contribution is still capped at ML_BLEND_WEIGHT (30%)
    to prevent a single model from dominating the proven matrix signal.
    """
    ml_prob = predict_hv_probability(card_boost, drafts, is_pitcher)
    if ml_prob is None:
        return matrix_rate

    blended = (1.0 - ML_BLEND_WEIGHT) * matrix_rate + ML_BLEND_WEIGHT * ml_prob
    return blended
