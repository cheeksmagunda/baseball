"""V12 trait scoring weights.

Calibration is manual — change defaults here directly when the audit on the
historical corpus shows a different distribution works better.  No DB
persistence, no /api/calibration/weights endpoint, no save/load: the live
pipeline imports `ScoringWeights()` and uses these values verbatim.

V12.2 weights remove env / trait double-counting (matchup_quality,
lineup_position, ballpark_factor were all reading the same signals env
already scored).
"""

from dataclasses import dataclass, field


@dataclass
class PitcherWeights:
    ace_status: float = 30.0
    k_rate: float = 35.0
    matchup_quality: float = 0.0       # zeroed — env handles opp OPS / K%
    recent_form: float = 20.0
    era_whip: float = 15.0

    @property
    def total_max(self) -> float:
        return self.ace_status + self.k_rate + self.matchup_quality + self.recent_form + self.era_whip


@dataclass
class BatterWeights:
    power_profile: float = 40.0
    matchup_quality: float = 0.0       # zeroed — env handles opp ERA / WHIP
    lineup_position: float = 0.0       # zeroed — env handles batting_order
    recent_form: float = 25.0
    ballpark_factor: float = 0.0       # zeroed — env handles park HR / wind / temp
    hot_streak: float = 25.0
    speed_component: float = 10.0

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
