"""Configurable scoring weights with defaults based on HV trait analysis."""

import json
from dataclasses import dataclass, field, asdict
from datetime import date

from sqlalchemy.orm import Session

from app.models.calibration import WeightHistory


@dataclass
class PitcherWeights:
    """V12.2: trait weights rebalanced to remove env double-counting.

    matchup_quality (opp OPS / K%) was 20 pts — but env_factor ALSO uses
    opp OPS in compute_pitcher_env_score.  Multiplying env × trait both
    weighted on the same signal amplified noise.  Zeroed and the points
    redistributed to ace_status / k_rate / recent_form / era_whip
    (independent intrinsic-talent signals).
    """
    ace_status: float = 30.0           # was 25.0 (+5 from matchup)
    k_rate: float = 35.0               # was 25.0 (+10 from matchup) — Statcast kinematics
    matchup_quality: float = 0.0       # was 20.0 — DOUBLE-COUNTED with env
    recent_form: float = 20.0          # was 15.0 (+5 from matchup)
    era_whip: float = 15.0             # unchanged

    @property
    def total_max(self) -> float:
        return self.ace_status + self.k_rate + self.matchup_quality + self.recent_form + self.era_whip


@dataclass
class BatterWeights:
    """V12.2: trait weights rebalanced to remove env double-counting.

    Three batter traits double-counted with env scoring:
      * matchup_quality (opp ERA / WHIP / K9 / hand-split / xwOBA-against)
        — env_factor uses opp ERA + WHIP, plus the hand-split signal was
        marginal in the audit (RHP HV=49.9% vs LHP HV=45.0%, only 5pp).
      * lineup_position (batting order) — env also uses batting_order.
      * ballpark_factor (park HR + wind + temp) — env captures all three.

    Zeroed and points redistributed to power_profile (Statcast exit-velo,
    barrel%, hard-hit%, xwOBA — intrinsic talent), recent_form (last 7
    games — intrinsic), hot_streak (intrinsic), speed_component (intrinsic).
    These are the only signals truly INDEPENDENT of env.
    """
    power_profile: float = 40.0        # was 25.0 (+15 from matchup/park)
    matchup_quality: float = 0.0       # was 20.0 — DOUBLE-COUNTED
    lineup_position: float = 0.0       # was 15.0 — DOUBLE-COUNTED
    recent_form: float = 25.0          # was 15.0 (+10 from matchup/park)
    ballpark_factor: float = 0.0       # was 10.0 — DOUBLE-COUNTED
    hot_streak: float = 25.0           # was 10.0 (+15 from lineup/matchup)
    speed_component: float = 10.0      # was 5.0 (+5)

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
