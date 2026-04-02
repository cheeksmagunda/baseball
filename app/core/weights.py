"""Configurable scoring weights with defaults based on HV trait analysis."""

import json
from dataclasses import dataclass, field, asdict
from datetime import date

from sqlalchemy.orm import Session

from app.models.calibration import WeightHistory


@dataclass
class PitcherWeights:
    ace_status: float = 25.0
    k_rate: float = 25.0
    matchup_quality: float = 20.0
    recent_form: float = 15.0
    era_whip: float = 15.0

    @property
    def total_max(self) -> float:
        return self.ace_status + self.k_rate + self.matchup_quality + self.recent_form + self.era_whip


@dataclass
class BatterWeights:
    power_profile: float = 25.0
    matchup_quality: float = 20.0
    lineup_position: float = 15.0
    recent_form: float = 15.0
    ballpark_factor: float = 10.0
    hot_streak: float = 10.0
    speed_component: float = 5.0

    @property
    def total_max(self) -> float:
        return (
            self.power_profile + self.matchup_quality + self.lineup_position
            + self.recent_form + self.ballpark_factor + self.hot_streak
            + self.speed_component
        )


@dataclass
class ScoringWeights:
    pitcher: PitcherWeights = field(default_factory=PitcherWeights)
    batter: BatterWeights = field(default_factory=BatterWeights)


def get_current_weights(db: Session | None = None) -> ScoringWeights:
    """Load weights from DB if available, otherwise return defaults."""
    weights = ScoringWeights()
    if db is None:
        return weights

    for player_type in ("pitcher", "batter"):
        row = (
            db.query(WeightHistory)
            .filter(WeightHistory.player_type == player_type)
            .order_by(WeightHistory.effective_date.desc())
            .first()
        )
        if row:
            data = json.loads(row.weights_json)
            if player_type == "pitcher":
                for k, v in data.items():
                    if hasattr(weights.pitcher, k):
                        setattr(weights.pitcher, k, v)
            else:
                for k, v in data.items():
                    if hasattr(weights.batter, k):
                        setattr(weights.batter, k, v)

    return weights


def save_weights(db: Session, weights: ScoringWeights, notes: str = ""):
    """Persist current weights to history."""
    today = date.today()
    for player_type, w in [("pitcher", weights.pitcher), ("batter", weights.batter)]:
        db.add(WeightHistory(
            effective_date=today,
            player_type=player_type,
            weights_json=json.dumps(asdict(w)),
            notes=notes,
        ))
    db.commit()
