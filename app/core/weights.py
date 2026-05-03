"""V12 trait scoring weights.

Calibration is manual — change defaults here directly when the audit on the
historical corpus shows a different distribution works better.  No DB
persistence, no /api/calibration/weights endpoint, no save/load: the live
pipeline imports `ScoringWeights()` and uses these values verbatim.

V12.2: matchup_quality, lineup_position, ballpark_factor removed.  Env
scoring is the single source of truth for opp ERA/WHIP, batting order, and
park/wind/temp signals — those traits were a DRY violation.
"""

from dataclasses import dataclass, field


@dataclass
class PitcherWeights:
    ace_status: float = 30.0
    k_rate: float = 35.0
    recent_form: float = 20.0
    era_whip: float = 15.0

    @property
    def total_max(self) -> float:
        return self.ace_status + self.k_rate + self.recent_form + self.era_whip


@dataclass
class BatterWeights:
    power_profile: float = 40.0
    recent_form: float = 25.0
    hot_streak: float = 25.0
    speed_component: float = 10.0

    @property
    def total_max(self) -> float:
        return self.power_profile + self.recent_form + self.hot_streak + self.speed_component


@dataclass
class ScoringWeights:
    pitcher: PitcherWeights = field(default_factory=PitcherWeights)
    batter: BatterWeights = field(default_factory=BatterWeights)
