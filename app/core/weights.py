"""V12 trait scoring weights.

V16 Phase 2: dataclass field defaults are populated from constants in
app/core/constants.py so the audit harness can sweep them via BO_OVERRIDE
without source edits.  Single source of truth: change PITCHER_WEIGHT_*
or BATTER_WEIGHT_* in constants.py and BOTH the live runtime and the
calibration harness pick up the change.

Calibration is manual — change defaults in constants.py directly when
the audit shows a different distribution works better.  No DB
persistence, no /api/calibration/weights endpoint, no save/load: the
live pipeline imports `ScoringWeights()` and uses these values verbatim.

V12.2: matchup_quality, lineup_position, ballpark_factor removed.  Env
scoring is the single source of truth for opp ERA/WHIP, batting order,
and park/wind/temp signals — those traits were a DRY violation.
"""

from dataclasses import dataclass, field

from app.core import constants as _C


def _pitcher_default() -> "PitcherWeights":
    return PitcherWeights(
        ace_status=_C.PITCHER_WEIGHT_ACE_STATUS,
        k_rate=_C.PITCHER_WEIGHT_K_RATE,
        recent_form=_C.PITCHER_WEIGHT_RECENT_FORM,
        era_whip=_C.PITCHER_WEIGHT_ERA_WHIP,
    )


def _batter_default() -> "BatterWeights":
    return BatterWeights(
        offensive_profile=_C.BATTER_WEIGHT_OFFENSIVE_PROFILE,
        recent_form=_C.BATTER_WEIGHT_RECENT_FORM,
        hot_streak=_C.BATTER_WEIGHT_HOT_STREAK,
        speed_component=_C.BATTER_WEIGHT_SPEED_COMPONENT,
    )


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
    offensive_profile: float = 40.0
    recent_form: float = 25.0
    hot_streak: float = 25.0
    speed_component: float = 10.0

    @property
    def total_max(self) -> float:
        return self.offensive_profile + self.recent_form + self.hot_streak + self.speed_component


@dataclass
class ScoringWeights:
    pitcher: PitcherWeights = field(default_factory=_pitcher_default)
    batter: BatterWeights = field(default_factory=_batter_default)
