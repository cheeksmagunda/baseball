"""
Filter Strategy: "Filter, Not Forecast" — the five-filter pipeline.

This is the core strategic engine from the Master Strategy Document.
We do NOT predict RS. We identify conditions under which high RS is
most likely to emerge, then select from that filtered pool.

Five filters applied sequentially:
  1. Slate Architecture — classify the day type
  2. Environmental Advantage — who has the conditions?
  3. Ownership Leverage — who is the crowd ignoring?
  4. Boost Optimization — how to allocate boosts?
  5. Lineup Construction — slot sequencing

References strategy doc sections §4.1–§4.5.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy.orm import Session

from app.core.constants import (
    PARK_HR_FACTORS,
    PITCHER_POSITIONS,
    BATTER_POSITIONS,
    SLOT_MULTIPLIERS,
    TINY_SLATE_MAX_GAMES,
    PITCHER_DAY_MIN_QUALITY_SP,
    HITTER_DAY_MIN_HIGH_TOTAL,
    HITTER_DAY_VEGAS_TOTAL_THRESHOLD,
    BLOWOUT_MONEYLINE_THRESHOLD,
    BLOWOUT_MIN_GAMES_FOR_STACK_DAY,
    ENV_PASS_THRESHOLD,
    MIN_GAMES_REPRESENTED,
    SAME_GAME_EXCESS_PENALTY,
    STACK_MIN_PLAYERS,
    STACK_MAX_PLAYERS,
    MIN_SCORE_THRESHOLD,
    MIN_SCORE_PENALTY_FLOOR,
    PITCHER_ENV_WEAK_OPP_OPS,
    PITCHER_ENV_WEAK_OPP_K_PCT,
    PITCHER_ENV_MIN_K_PER_9,
    PITCHER_ENV_FRIENDLY_PARK,
    BATTER_ENV_HIGH_VEGAS_TOTAL,
    BATTER_ENV_WEAK_PITCHER_ERA,
    BATTER_ENV_TOP_LINEUP,
    BATTER_ENV_WEAK_BULLPEN_ERA,
    DEBUT_RETURN_EV_BONUS,
    POPULARITY_FADE_PENALTY,
    POPULARITY_TARGET_BONUS,
    MOONSHOT_FADE_PENALTY,
    MOONSHOT_NEUTRAL_PENALTY,
    MOONSHOT_TARGET_BONUS,
    MOONSHOT_SHARP_BONUS_MAX,
    MOONSHOT_EXPLOSIVE_BONUS_MAX,
    MOONSHOT_SAME_TEAM_PENALTY,
    GHOST_DRAFT_THRESHOLD,
    LOW_DRAFT_THRESHOLD,
    CHALK_DRAFT_THRESHOLD,
    MEGA_CHALK_DRAFT_THRESHOLD,
    GHOST_BOOST_SYNERGY_MIN_BOOST,
    MEGA_GHOST_BOOST_MAX_DRAFTS,
    MAX_MEGA_CHALK_IN_LINEUP,
    MIN_GHOST_IN_LINEUP,
    MAX_PITCHERS_IN_LINEUP,
    REQUIRED_PITCHERS_IN_LINEUP,
    PITCHER_ANCHOR_SLOT,
    BOOST_CONCENTRATION_THRESHOLD,
    BOOST_CONCENTRATION_PENALTY,
    GHOST_ENFORCE_SWAP_THRESHOLD,
    MAX_PLAYERS_PER_TEAM,
    MAX_PLAYERS_PER_GAME,
    MAX_OPPONENTS_SAME_GAME,
    STACK_BONUS,
    DNP_RISK_PENALTY,
    DNP_UNKNOWN_PENALTY,
    DNP_GHOST_UNKNOWN_PENALTY,
    ENV_UNKNOWN_COUNT_THRESHOLD,
    BOOST_QUALITY_THRESHOLD,
    BOOSTED_POOL_FULL_THRESHOLD,
    # Cross-lineup correlation + tiebreaker constants
    CORRELATION_GHOST_MIN_PLAYERS,
    CORRELATION_EV_BONUS,
    CORRELATION_EV_BONUS_3PLUS,
    MOONSHOT_CORRELATION_TEAMMATE_BONUS,
    ENV_TIEBREAKER_BONUS_MAX,
    ENV_TIEBREAKER_HV_THRESHOLD,
    # Pitcher-specific FADE moderation + scarcity tiebreaker
    PITCHER_FADE_PENALTY,
    MOONSHOT_PITCHER_FADE_PENALTY,
    DRAFT_SCARCITY_TIEBREAKER_MAX,
)
from app.core.utils import BASE_MULTIPLIER, get_trait_score
from app.services.popularity import PopularityClass

logger = logging.getLogger(__name__)


# V5.0: compute_dynamic_pitcher_cap() removed.  Every lineup has exactly
# REQUIRED_PITCHERS_IN_LINEUP pitchers (see app/core/constants.py).  The
# pitcher is selected first (highest filter_ev), anchors Slot 1, and its
# game is blocked for all batter picks in the same lineup.


def _identify_correlation_groups(
    candidates: list,
) -> dict[str, list]:
    """Deprecated — ownership-tier-based correlation was removed with card_boost
    and draft-count inputs.  Returns an empty dict so callers that still invoke
    this function see "no correlation teams" and skip the correlation bonus.
    Cross-lineup correlation now falls out of the same-team Moonshot penalty
    and the team/game diversification caps.
    """
    return {}


# ---------------------------------------------------------------------------
# Filter 1: Slate Classification (§4.2 Filter 1)
# ---------------------------------------------------------------------------

class SlateType(str, Enum):
    TINY = "tiny"
    PITCHER_DAY = "pitcher_day"
    HITTER_DAY = "hitter_day"
    STANDARD = "standard"


@dataclass
class StackableGame:
    """A game identified as a blowout/stacking candidate."""
    game_id: int | str | None = None
    favored_team: str = ""
    moneyline: int | None = None
    vegas_total: float | None = None
    opp_starter_era: float | None = None


@dataclass
class SlateClassification:
    slate_type: SlateType
    game_count: int
    quality_sp_matchups: int = 0
    high_total_games: int = 0
    blowout_games: int = 0
    stackable_games: list[StackableGame] = field(default_factory=list)
    reason: str = ""


def classify_slate(
    game_count: int,
    games: list[dict] | None = None,
) -> SlateClassification:
    """
    Classify the slate BEFORE looking at any individual player.

    §3 Slate Classification:
    - Tiny (1-3 games): limited pool, heavy team-stack
    - Pitcher Day (4+ quality SP matchups): go 4-5 pitchers
    - Hitter/Stack Day (4+ high O/U OR 1+ blowout game): stack the favorite
    - Standard: 2-3P + 2-3 hitters

    Key insight: "Read the slate, don't default to pitchers."
    Hitter/stack days are 38% of slates (most common winning type).
    Blowout games (moneyline ≥ -200) are prime stacking candidates.
    """
    games = games or []

    quality_sp = 0
    high_total = 0
    blowout_games = 0
    stackable: list[StackableGame] = []

    for g in games:
        vt = g.get("vegas_total")
        if vt is not None and vt >= HITTER_DAY_VEGAS_TOTAL_THRESHOLD:
            high_total += 1

        # Blowout detection (§2 Pillar 2): moneyline ≥ -200 = projected blowout
        home_ml = g.get("home_moneyline")
        away_ml = g.get("away_moneyline")
        if home_ml is not None and home_ml <= BLOWOUT_MONEYLINE_THRESHOLD:
            blowout_games += 1
            stackable.append(StackableGame(
                game_id=g.get("game_id"),
                favored_team=g.get("home_team", ""),
                moneyline=home_ml,
                vegas_total=vt,
                opp_starter_era=g.get("away_starter_era"),
            ))
        elif away_ml is not None and away_ml <= BLOWOUT_MONEYLINE_THRESHOLD:
            blowout_games += 1
            stackable.append(StackableGame(
                game_id=g.get("game_id"),
                favored_team=g.get("away_team", ""),
                moneyline=away_ml,
                vegas_total=vt,
                opp_starter_era=g.get("home_starter_era"),
            ))

        # Check home starter as quality matchup
        h_era = g.get("home_starter_era")
        h_k9 = g.get("home_starter_k_per_9")
        a_ops = g.get("away_team_ops")
        if h_era is not None and h_era < 3.5:
            if a_ops is not None and a_ops < PITCHER_ENV_WEAK_OPP_OPS:
                quality_sp += 1
            elif h_k9 is not None and h_k9 >= PITCHER_ENV_MIN_K_PER_9:
                quality_sp += 1

        # Check away starter as quality matchup
        a_era = g.get("away_starter_era")
        a_k9 = g.get("away_starter_k_per_9")
        h_ops = g.get("home_team_ops")
        if a_era is not None and a_era < 3.5:
            if h_ops is not None and h_ops < PITCHER_ENV_WEAK_OPP_OPS:
                quality_sp += 1
            elif a_k9 is not None and a_k9 >= PITCHER_ENV_MIN_K_PER_9:
                quality_sp += 1

    # Sort stackable games by moneyline strength (most negative = biggest favorite)
    stackable.sort(key=lambda s: s.moneyline if s.moneyline is not None else 0)

    # Classification logic — check hitter/stack BEFORE pitcher day
    # because hitter/stack days are 38% vs pitcher days 23%
    if game_count <= TINY_SLATE_MAX_GAMES:
        return SlateClassification(
            slate_type=SlateType.TINY,
            game_count=game_count,
            quality_sp_matchups=quality_sp,
            high_total_games=high_total,
            blowout_games=blowout_games,
            stackable_games=stackable,
            reason=f"Tiny slate ({game_count} games). Stack the favorite.",
        )

    # Blowout games trigger hitter/stack day even without high O/U counts
    if (high_total >= HITTER_DAY_MIN_HIGH_TOTAL
            or blowout_games >= BLOWOUT_MIN_GAMES_FOR_STACK_DAY):
        stack_reason = []
        if high_total >= HITTER_DAY_MIN_HIGH_TOTAL:
            stack_reason.append(f"{high_total} high O/U games")
        if blowout_games > 0:
            teams = [s.favored_team for s in stackable]
            stack_reason.append(f"{blowout_games} blowout game(s): {', '.join(teams)}")
        return SlateClassification(
            slate_type=SlateType.HITTER_DAY,
            game_count=game_count,
            quality_sp_matchups=quality_sp,
            high_total_games=high_total,
            blowout_games=blowout_games,
            stackable_games=stackable,
            reason=f"Hitter/Stack day: {'; '.join(stack_reason)}. Stack 3-4 from favorite + 1-2 diversifiers.",
        )

    if quality_sp >= PITCHER_DAY_MIN_QUALITY_SP:
        return SlateClassification(
            slate_type=SlateType.PITCHER_DAY,
            game_count=game_count,
            quality_sp_matchups=quality_sp,
            high_total_games=high_total,
            blowout_games=blowout_games,
            stackable_games=stackable,
            reason=f"Pitcher day: {quality_sp} quality SP matchups. Go 4-5 pitchers.",
        )

    return SlateClassification(
        slate_type=SlateType.STANDARD,
        game_count=game_count,
        quality_sp_matchups=quality_sp,
        high_total_games=high_total,
        blowout_games=blowout_games,
        stackable_games=stackable,
        reason=f"Standard slate ({game_count} games). 2-3 pitchers + 2-3 hitters.",
    )


# ---------------------------------------------------------------------------
# Filter 2: Environmental Advantage (§4.2 Filter 2)
# ---------------------------------------------------------------------------

@dataclass
class EnvironmentalProfile:
    """Pre-game environmental factors for a single player."""
    player_name: str
    team: str
    position: str
    is_pitcher: bool = False
    env_score: float = 0.5  # 0-1.0; >0.5 = passes environmental filter
    env_factors: list[str] = field(default_factory=list)
    env_unknown_count: int = 0  # how many factors were missing (unknown vs bad)

    # Pitcher-specific
    opp_team_ops: float | None = None
    opp_team_k_pct: float | None = None
    pitcher_k_per_9: float | None = None
    park_factor: float | None = None
    is_home: bool = False

    # Batter-specific
    vegas_total: float | None = None
    opp_pitcher_era: float | None = None
    platoon_advantage: bool = False
    batting_order: int | None = None

    # Shared
    is_debut_or_return: bool = False


def compute_pitcher_env_score(
    opp_team_ops: float | None = None,
    opp_team_k_pct: float | None = None,
    pitcher_k_per_9: float | None = None,
    park_team: str | None = None,
    is_home: bool = False,
    is_debut_or_return: bool = False,
) -> tuple[float, list[str]]:
    """
    Compute environmental score for a pitcher (0-1.0).

    Strategy doc §4.2 Filter 2 pitcher conditions:
    - Facing bottom-10 offense by K% or wOBA/OPS
    - Pitching in a pitcher-friendly park
    - Pitching at home (slight edge)
    - High K/9 rate (strikeouts drive RS)
    - Being the probable starter
    """
    score = 0.0
    factors = []
    max_score = 5.0  # 5 factors, each 0-1

    # 1. Weak opponent offense (OPS)
    if opp_team_ops is not None:
        if opp_team_ops < PITCHER_ENV_WEAK_OPP_OPS:
            score += 1.0
            factors.append(f"Weak opponent OPS ({opp_team_ops:.3f})")
        elif opp_team_ops < 0.730:
            score += 0.5
            factors.append(f"Below-avg opponent OPS ({opp_team_ops:.3f})")

    # 2. High-K opponent
    if opp_team_k_pct is not None:
        if opp_team_k_pct >= PITCHER_ENV_WEAK_OPP_K_PCT:
            score += 1.0
            factors.append(f"High-K opponent ({opp_team_k_pct:.1%})")
        elif opp_team_k_pct >= 0.22:
            score += 0.5

    # 3. K upside (pitcher's own K/9)
    if pitcher_k_per_9 is not None:
        if pitcher_k_per_9 >= PITCHER_ENV_MIN_K_PER_9:
            score += 1.0
            factors.append(f"K upside (K/9={pitcher_k_per_9:.1f})")
        elif pitcher_k_per_9 >= 7.0:
            score += 0.5

    # 4. Pitcher-friendly park
    if park_team:
        pf = PARK_HR_FACTORS.get(park_team, 1.0)
        if pf <= PITCHER_ENV_FRIENDLY_PARK:
            score += 1.0
            factors.append(f"Pitcher-friendly park ({park_team}, factor={pf:.2f})")
        elif pf <= 1.05:
            score += 0.5

    # 5. Home field
    if is_home:
        score += 0.5
        factors.append("Home field")

    # Debut/return bonus
    if is_debut_or_return:
        score += 0.5
        factors.append("Debut/return premium")

    env_score = min(1.0, score / max_score)
    return env_score, factors


def compute_batter_env_score(
    vegas_total: float | None = None,
    opp_pitcher_era: float | None = None,
    platoon_advantage: bool = False,
    batting_order: int | None = None,
    park_team: str | None = None,
    is_debut_or_return: bool = False,
    wind_speed_mph: float | None = None,
    wind_direction: str | None = None,
    temperature_f: int | None = None,
    team_moneyline: int | None = None,
    opp_bullpen_era: float | None = None,
) -> tuple[float, list[str], int]:
    """
    Compute environmental score for a batter (0-1.0).

    Returns a third value, `unknown_count`, tracking how many environmental
    factors were missing (None) vs. confirmed bad.  This enables the pipeline to
    distinguish "data scarcity" from "genuinely bad conditions" — critical for
    ghost-tier players where missing data is expected, not a negative signal.

    §4 batter filters:
    - Playing in high Vegas total game (O/U >= 8.5)
    - Facing a weak opposing starter (high ERA)
    - Having a platoon advantage
    - Batting in top 5 of lineup
    - Hitter-friendly park or favorable weather
    - Team is moneyline favorite
    - Vulnerable opposing bullpen (high bullpen ERA)
    """
    score = 0.0
    factors = []
    unknown_count = 0  # track missing data factors
    max_score = 7.0  # 7 factors (added bullpen vulnerability)

    # 1. High Vegas total (run environment)
    if vegas_total is not None:
        if vegas_total >= BATTER_ENV_HIGH_VEGAS_TOTAL:
            score += 1.0
            factors.append(f"High-run environment (O/U={vegas_total:.1f})")
        elif vegas_total >= 7.5:
            score += 0.5
    else:
        unknown_count += 1

    # 2. Weak opposing starter
    if opp_pitcher_era is not None:
        if opp_pitcher_era >= BATTER_ENV_WEAK_PITCHER_ERA:
            score += 1.0
            factors.append(f"Weak opposing starter (ERA={opp_pitcher_era:.2f})")
        elif opp_pitcher_era >= 4.0:
            score += 0.5
    else:
        unknown_count += 1

    # 3. Platoon advantage
    if platoon_advantage:
        score += 1.0
        factors.append("Platoon advantage")

    # 4. Top of lineup (top 5)
    # Missing batting order is tracked as unknown, not penalized as bad.
    # The DNP risk penalty in _compute_filter_ev() handles the lineup risk
    # separately with ghost-awareness (DNP_GHOST_UNKNOWN_PENALTY).
    if batting_order is not None:
        if batting_order <= BATTER_ENV_TOP_LINEUP:
            score += 1.0
            factors.append(f"Top of lineup (bats #{batting_order})")
        elif batting_order <= 6:
            score += 0.5
    else:
        unknown_count += 1

    # 5. Hitter-friendly park + favorable weather (combined, capped at 1.0)
    f5 = 0.0
    if park_team:
        pf = PARK_HR_FACTORS.get(park_team, 1.0)
        if pf >= 1.05:
            f5 = 1.0
            factors.append(f"Hitter-friendly park ({park_team}, factor={pf:.2f})")
        elif pf >= 1.0:
            f5 = 0.5

    if wind_speed_mph is not None and wind_speed_mph >= 10 and wind_direction:
        direction_upper = wind_direction.upper()
        if any(d in direction_upper for d in ("OUT", "L TO R", "R TO L", "OUT TO CF")):
            f5 = min(1.0, f5 + 0.5)
            factors.append(f"Wind blowing out ({wind_speed_mph:.0f} mph)")

    if temperature_f is not None and temperature_f >= 80:
        f5 = min(1.0, f5 + 0.2)
        factors.append(f"Warm conditions ({temperature_f}°F)")

    score += f5

    # 6. Team is moneyline favorite
    if team_moneyline is not None:
        if team_moneyline <= BLOWOUT_MONEYLINE_THRESHOLD:
            score += 1.0
            factors.append(f"Heavy favorite (ML={team_moneyline})")
        elif team_moneyline < -120:
            score += 0.5
            factors.append(f"Moneyline favorite (ML={team_moneyline})")
    else:
        unknown_count += 1

    # 7. Vulnerable opposing bullpen (high ERA = late-game upside)
    if opp_bullpen_era is not None:
        if opp_bullpen_era >= BATTER_ENV_WEAK_BULLPEN_ERA:
            score += 1.0
            factors.append(f"Vulnerable bullpen (ERA={opp_bullpen_era:.2f})")
        elif opp_bullpen_era >= BATTER_ENV_WEAK_BULLPEN_ERA - 0.5:
            score += 0.5
            factors.append(f"Below-avg bullpen (ERA={opp_bullpen_era:.2f})")
    else:
        unknown_count += 1

    # Debut/return bonus
    if is_debut_or_return:
        score += 0.5
        factors.append("Debut/return premium")

    if unknown_count > 0:
        factors.append(f"{unknown_count} unknown factor(s) (data scarcity, not bad env)")

    env_score = min(1.0, score / max_score)
    return env_score, factors, unknown_count



# ---------------------------------------------------------------------------
# Filter 4+5: Boost Optimization & Lineup Construction
# These are integrated into the FilterStrategyOptimizer below.
# ---------------------------------------------------------------------------

@dataclass
class FilteredCandidate:
    """A player card that has passed through Filters 1-3."""
    player_name: str
    team: str
    position: str
    card_boost: float
    total_score: float  # 0-100 from scoring engine
    env_score: float    # 0-1.0 from environmental filter
    env_factors: list[str] = field(default_factory=list)
    env_unknown_count: int = 0  # how many env factors were missing data
    popularity: PopularityClass = PopularityClass.NEUTRAL  # web-scraped
    is_debut_or_return: bool = False
    game_id: int | str | None = None  # for diversification tracking
    is_pitcher: bool = False
    sharp_score: float = 0.0
    drafts: int | None = None
    is_most_drafted_3x: bool = False  # 92% batter bust rate (V5.0 retrain) — hard-excluded from S5
    traits: list = field(default_factory=list)  # TraitScore list from scoring engine
    batting_order: int | None = None  # 1-9 if confirmed in lineup, None = DNP risk
    is_in_blowout_game: bool = False  # set by run_filter_strategy before EV computation
    total_slate_drafts: int | None = None  # sum of all drafts on the slate (for dynamic thresholds)
    correlation_bonus: float = 1.0  # cross-lineup correlation multiplier (set pre-EV)

    # Computed by the optimizer
    filter_ev: float = 0.0

    @property
    def is_ghost(self) -> bool:
        """True if this player is in the ghost ownership tier (< GHOST_DRAFT_THRESHOLD drafts)."""
        return self.drafts is not None and self.drafts < GHOST_DRAFT_THRESHOLD


@dataclass
class FilterSlotAssignment:
    slot_index: int
    slot_mult: float
    candidate: FilteredCandidate
    expected_slot_value: float


@dataclass
class FilterOptimizedLineup:
    slots: list[FilterSlotAssignment]
    total_expected_value: float
    strategy: str
    slate_classification: SlateClassification
    composition: dict = field(default_factory=dict)  # {pitchers: N, hitters: N}
    warnings: list[str] = field(default_factory=list)


def _popularity_ev_adjustment(popularity: PopularityClass, is_pitcher: bool = False) -> float:
    """LEGACY shim — V6.0 uses get_rs_condition_factor() instead.

    Pitchers get a lighter FADE penalty (15% vs 25%).  Pitchers control
    their own environment — high draft count reflects real ERA/K-rate data,
    not media hype.  The crowd is structurally less wrong about pitchers than
    batters: pitcher outcomes depend on one player, batter outcomes depend on
    team context.  Apr 11: Suarez (2.2k drafts, FADE) delivered RS 5.7 and
    appeared in 6/6 top lineups.
    """
    if popularity == PopularityClass.FADE:
        if is_pitcher:
            return PITCHER_FADE_PENALTY
        return POPULARITY_FADE_PENALTY
    if popularity == PopularityClass.TARGET:
        return POPULARITY_TARGET_BONUS
    return 1.0


def _compute_dnp_adjustment(candidate: FilteredCandidate) -> float:
    """Compute DNP risk adjustment for batters only.

    Pitchers at this stage are already filtered to confirmed probable starters.
    For batters: a known batting_order = posted lineup, full confidence.
    Missing batting_order with many other env fields missing = slate data
    not yet published (DNP-unknown, light penalty).  Missing batting_order
    with a full env context = lineup posted without the player (DNP-confirmed,
    heavier penalty).
    """
    if candidate.is_pitcher or candidate.batting_order is not None:
        return 1.0
    if candidate.env_unknown_count >= ENV_UNKNOWN_COUNT_THRESHOLD:
        return DNP_UNKNOWN_PENALTY
    return DNP_RISK_PENALTY


def _compute_base_ev(
    candidate: FilteredCandidate,
    pop_factor: float,
) -> float:
    """Compute the shared base EV used by both Starting 5 and Moonshot.

    V6.0 "Popularity-First Side Analysis" — the EV formula is restructured
    so the web-scraped popularity signal is the DOMINANT term, not a modifier.

    Empirical basis (20 dates, 2026-03-25 → 2026-04-13):
      Batter+TARGET:  avg RS 3.57, HV rate 73.6%
      Batter+FADE:    avg RS 0.98, HV rate  9.6%
      Pitcher+TARGET: avg RS 4.36, HV rate 44.7%
      Pitcher+FADE:   avg RS 3.09, HV rate 19.3%

    The three signals, in order of influence:

      1. pop_factor   — from RS_CONDITION_MATRIX via get_rs_condition_factor().
                        Range: 0.275–1.00 for batters, 0.71–1.00 for pitchers.
                        This is the primary ranking driver — a TARGET batter
                        starts at 3.6x the EV of a FADE batter.

      2. env_factor   — game conditions (Vegas total, opposing starter ERA,
                        ballpark, weather, batting order).  Range: 0.60–1.40.
                        Second-strongest signal — good matchups matter, but
                        can't rescue a FADE.

      3. trait_factor  — intrinsic player quality from the scoring engine
                        (season stats × matchup context, 0-100).  Compressed
                        to range 0.75–1.25 so traits are TIEBREAKERS, not
                        dominators.  High trait scores correlate with fame,
                        which is exactly what the crowd over-drafts.

    Plus contextual multipliers: stack_bonus, debut_bonus, dnp_adj.

    Formula:
        base_ev = pop_factor × env_factor × trait_factor
                  × stack_bonus × debut_bonus × dnp_adj × 100
    """
    from app.core.constants import (
        TRAIT_MODIFIER_FLOOR,
        TRAIT_MODIFIER_CEILING,
        ENV_MODIFIER_FLOOR,
        ENV_MODIFIER_CEILING,
    )

    # trait_score is 0-100 from the scoring engine.  Compress to
    # TRAIT_MODIFIER_FLOOR–TRAIT_MODIFIER_CEILING (default 0.75–1.25) so
    # traits differentiate within a popularity tier but can't override it.
    raw_trait = max(candidate.total_score, 15.0) / 100.0  # 0.15–1.0
    trait_factor = TRAIT_MODIFIER_FLOOR + (raw_trait - 0.15) * (
        TRAIT_MODIFIER_CEILING - TRAIT_MODIFIER_FLOOR
    ) / (1.0 - 0.15)
    trait_factor = max(TRAIT_MODIFIER_FLOOR, min(TRAIT_MODIFIER_CEILING, trait_factor))

    # env_score is already 0-1 from compute_batter_env_score /
    # compute_pitcher_env_score.  Scale to ENV_MODIFIER_FLOOR–ENV_MODIFIER_CEILING.
    raw_env = max(candidate.env_score, 0.0)
    env_factor = ENV_MODIFIER_FLOOR + raw_env * (ENV_MODIFIER_CEILING - ENV_MODIFIER_FLOOR)
    env_factor = max(ENV_MODIFIER_FLOOR, min(ENV_MODIFIER_CEILING, env_factor))

    stack_bonus = STACK_BONUS if candidate.is_in_blowout_game else 1.0
    debut_bonus = DEBUT_RETURN_EV_BONUS if candidate.is_debut_or_return else 1.0
    dnp_adj = _compute_dnp_adjustment(candidate)

    return (
        pop_factor
        * env_factor
        * trait_factor
        * stack_bonus
        * debut_bonus
        * dnp_adj
        * 100.0
    )


def _compute_filter_ev(candidate: FilteredCandidate) -> float:
    """Compute Starting 5 EV: popularity-first formula via RS condition matrix."""
    from app.services.condition_classifier import get_rs_condition_factor

    pop_factor = get_rs_condition_factor(
        candidate.popularity.value,
        is_pitcher=candidate.is_pitcher,
    )
    return _compute_base_ev(candidate, pop_factor)


def _build_team_stack(
    candidates: list[FilteredCandidate],
    stackable_games: list[StackableGame],
) -> list[FilteredCandidate] | None:
    """
    Build a team stack from ghost-ownership players on the favored team.

    §2 Pillar 2: "Stack FROM THE GHOST POOL."
    The winning OAK stack on 4/5 worked because the entire lineup was ghost-tier.
    The winning LAD stack on 4/6 worked because ghosts (Hernández 3, Rushing 1)
    were the differentiators — Ohtani (4900) was just the anchor everyone had.

    Returns 3-4 players from the best stackable team, or None if no viable stack.
    """
    if not stackable_games:
        return None

    for sg in stackable_games:
        team = sg.favored_team.upper()
        game_id = sg.game_id

        # Find all candidates from the favored team in this game
        team_candidates = [
            c for c in candidates
            if c.team.upper() == team
            and not c.is_pitcher  # stack hitters, not pitchers
        ]

        if len(team_candidates) < STACK_MIN_PLAYERS:
            continue

        # Sort by filter_ev — the popularity + env + trait signal is the only
        # input now that ownership and boost are out of the pipeline.
        team_candidates.sort(key=lambda c: c.filter_ev, reverse=True)
        stack = team_candidates[:STACK_MAX_PLAYERS]

        if len(stack) >= STACK_MIN_PLAYERS:
            logger.info(
                "Team stack: %d %s players (game_id: %s, ML: %s)",
                len(stack), team, game_id, sg.moneyline,
            )
            return stack

    return None


def _apply_s5_hard_exclusions(
    candidates: list[FilteredCandidate],
) -> list[FilteredCandidate]:
    """No-op — ownership-tier-based hard exclusions were removed with the
    drafts input.  Starting 5 trusts the EV ranking (popularity + env +
    traits).  The popularity FADE penalty already punishes over-drafted
    players; stacking a hard-exclusion on top of a 25% EV haircut was
    redundant.  Kept as a no-op for back-compat with run_filter_strategy.
    """
    return list(candidates)


def _enforce_composition(
    candidates: list[FilteredCandidate],
    slate_class: SlateClassification,
) -> list[FilteredCandidate]:
    """
    V6.0 pitcher-anchored composition: 1 pitcher + 4 batters.

    Retains V5.0's 1P+4B shape.  The popularity-first EV formula determines
    WHICH pitcher and WHICH batters fill those slots — but the composition
    is fixed at 1 SP (Slot 1) + 4 batters (Slots 2-5).

    Construction:
    1. Sort all candidates by filter_ev descending.
    2. Pick the highest-EV pitcher as anchor.  Its game_id is blocked.
    3. Fill 4 batter slots by filter_ev, respecting MAX_PLAYERS_PER_TEAM
       and MAX_PLAYERS_PER_GAME.
    """
    all_sorted = sorted(candidates, key=lambda c: c.filter_ev, reverse=True)

    # Phase 1: Select the anchor pitcher (highest-EV pitcher in the pool).
    anchor_pitcher = next((c for c in all_sorted if c.is_pitcher), None)
    if anchor_pitcher is None:
        raise ValueError(
            "Candidate pool contains no pitcher. "
            "Cannot build a lineup without an SP anchor."
        )

    blocked_game_id = anchor_pitcher.game_id

    # Phase 2: Batters ordered by filter_ev (popularity-first ranking).
    ordered_batters = [c for c in all_sorted if not c.is_pitcher]

    # Phase 3: Fill 4 batter slots.
    lineup: list[FilteredCandidate] = [anchor_pitcher]
    anchor_team = anchor_pitcher.team.upper()
    team_used: dict[str, int] = {anchor_team: 1}
    game_team_used: dict[tuple, int] = {}
    game_teams: dict[int | str | None, set[str]] = {}
    if blocked_game_id is not None:
        game_team_used[(blocked_game_id, anchor_team)] = 1
        game_teams[blocked_game_id] = {anchor_team}

    for c in ordered_batters:
        if len(lineup) == 5:
            break

        # Block the anchor pitcher's game (no negative correlation).
        if blocked_game_id is not None and c.game_id == blocked_game_id:
            continue

        team_key = c.team.upper()
        if team_used.get(team_key, 0) >= MAX_PLAYERS_PER_TEAM:
            continue

        if c.game_id is not None:
            gt_key = (c.game_id, team_key)
            if game_team_used.get(gt_key, 0) >= MAX_PLAYERS_PER_GAME:
                continue
            existing_teams = game_teams.get(c.game_id, set())
            opponent_teams = existing_teams - {team_key}
            if opponent_teams:
                opp_count = sum(
                    game_team_used.get((c.game_id, t), 0)
                    for t in opponent_teams
                )
                if opp_count >= MAX_OPPONENTS_SAME_GAME:
                    if team_key not in existing_teams:
                        continue
            game_team_used[gt_key] = game_team_used.get(gt_key, 0) + 1
            game_teams.setdefault(c.game_id, set()).add(team_key)

        team_used[team_key] = team_used.get(team_key, 0) + 1
        lineup.append(c)

    # Validate team/game caps.
    lineup = _validate_lineup_structure(
        lineup, ordered_batters, anchor_pitcher=anchor_pitcher
    )
    pitcher_count = sum(1 for c in lineup if c.is_pitcher)
    logger.info(
        "V6.0 composition: %dP/%dH — anchor=%s (EV=%.2f) (candidates: %d)",
        pitcher_count, len(lineup) - pitcher_count,
        anchor_pitcher.player_name, anchor_pitcher.filter_ev,
        len(candidates),
    )
    return lineup


def _validate_lineup_structure(
    lineup: list[FilteredCandidate],
    all_candidates_sorted: list[FilteredCandidate],
    anchor_pitcher: FilteredCandidate | None = None,
) -> list[FilteredCandidate]:
    """
    V6.0 validation — enforce team/game caps and 1-pitcher invariant.

    The anchor pitcher is protected from all swaps.  Replacement candidates
    are batters only (to preserve 1P+4B).
    """
    if len(lineup) < 5:
        return lineup

    anchor_idx: int | None = None
    if anchor_pitcher is not None:
        for i, c in enumerate(lineup):
            if c.player_name == anchor_pitcher.player_name:
                anchor_idx = i
                break

    def _protected(idx: int) -> bool:
        return anchor_idx is not None and idx == anchor_idx

    # Rule 1: Max N players per team — anchor exempt.
    from collections import Counter
    team_counts = Counter(c.team for c in lineup)
    for team, count in team_counts.items():
        if count > MAX_PLAYERS_PER_TEAM:
            team_indices = sorted(
                [i for i, c in enumerate(lineup)
                 if c.team == team and not _protected(i)],
                key=lambda i: lineup[i].filter_ev,
                reverse=True,
            )
            lineup_names = {c.player_name for c in lineup}
            anchor_game = anchor_pitcher.game_id if anchor_pitcher is not None else None
            for idx in team_indices[MAX_PLAYERS_PER_TEAM:]:
                current_team_counts = Counter(c.team for c in lineup)
                replacement = next(
                    (c for c in all_candidates_sorted
                     if c.player_name not in lineup_names
                     and not c.is_pitcher
                     and c.game_id != anchor_game
                     and current_team_counts.get(c.team, 0) < MAX_PLAYERS_PER_TEAM),
                    None,
                )
                if replacement:
                    removed_name = lineup[idx].player_name
                    lineup_names.discard(removed_name)
                    lineup[idx] = replacement
                    lineup_names.add(replacement.player_name)
                    logger.info(
                        "Team cap: replaced %s (%s) with %s (%s)",
                        removed_name, team,
                        replacement.player_name, replacement.team,
                    )

    # Rule 2: Max N players per game — anchor exempt.
    game_counts = Counter(c.game_id for c in lineup if c.game_id is not None)
    for game_id, count in game_counts.items():
        if count > MAX_PLAYERS_PER_GAME:
            game_indices = sorted(
                [i for i, c in enumerate(lineup)
                 if c.game_id == game_id and not _protected(i)],
                key=lambda i: lineup[i].filter_ev,
                reverse=True,
            )
            lineup_names = {c.player_name for c in lineup}
            anchor_game = anchor_pitcher.game_id if anchor_pitcher is not None else None
            for idx in game_indices[MAX_PLAYERS_PER_GAME:]:
                current_game_counts = Counter(
                    c.game_id for c in lineup if c.game_id is not None
                )
                replacement = next(
                    (c for c in all_candidates_sorted
                     if c.player_name not in lineup_names
                     and not c.is_pitcher
                     and c.game_id != anchor_game
                     and (c.game_id is None
                          or current_game_counts.get(c.game_id, 0) < MAX_PLAYERS_PER_GAME)),
                    None,
                )
                if replacement:
                    removed_name = lineup[idx].player_name
                    lineup_names.discard(removed_name)
                    lineup[idx] = replacement
                    lineup_names.add(replacement.player_name)
                    logger.info(
                        "Game cap: replaced %s (game=%s) with %s (game=%s)",
                        removed_name, game_id,
                        replacement.player_name, replacement.game_id,
                    )

    # Rule 3: Exactly 1 pitcher (sanity check).
    pitcher_count_final = sum(1 for c in lineup if c.is_pitcher)
    if pitcher_count_final != REQUIRED_PITCHERS_IN_LINEUP:
        logger.warning(
            "Pitcher-count invariant violated: expected %d, got %d in %s",
            REQUIRED_PITCHERS_IN_LINEUP, pitcher_count_final,
            [c.player_name for c in lineup],
        )

    return lineup


def _apply_game_diversification(
    lineup: list[FilteredCandidate],
) -> list[str]:
    """
    Check game diversification.

    Max 1 player per game is enforced during composition and validation.
    This function now serves as a safety-net warning if any violations leaked
    through, plus reports game spread for diagnostics.
    """
    warnings = []
    if not lineup:
        return warnings

    game_counts: dict[str | int | None, int] = {}
    for c in lineup:
        gid = c.game_id
        if gid is not None:
            game_counts[gid] = game_counts.get(gid, 0) + 1

    games_represented = len(game_counts) if game_counts else 0

    # Safety-net: warn if any game has more than the cap (shouldn't happen
    # after _enforce_composition + _validate_lineup_structure, but belt+suspenders).
    for gid, count in game_counts.items():
        if count > MAX_PLAYERS_PER_GAME:
            warnings.append(
                f"Game {gid} has {count} players (cap={MAX_PLAYERS_PER_GAME}). "
                f"Game diversification may not have been fully enforced."
            )
            # Soft penalty on excess as fallback
            game_players = sorted(
                [c for c in lineup if c.game_id == gid],
                key=lambda c: c.filter_ev,
            )
            for p in game_players[:count - MAX_PLAYERS_PER_GAME]:
                p.filter_ev *= SAME_GAME_EXCESS_PENALTY

    logger.info(
        "Game diversification: %d games represented across %d players",
        games_represented, len(lineup),
    )

    return warnings


def _apply_boost_diversification(
    lineup: list[FilteredCandidate],
) -> list[str]:
    """
    Check boost concentration across games (§4.2 Filter 4).

    "Don't put all boosted players in the same game. If that game is
    a 1-0 pitcher's duel, all your boosts become dead weight."

    If 3+ boosted players share the same game, apply a penalty
    to the 3rd+ boosted player (sorted by EV desc, top 2 untouched).
    """
    warnings = []
    if not lineup:
        return warnings

    boosted_by_game: dict[str | int | None, list[FilteredCandidate]] = {}
    for c in lineup:
        if c.card_boost >= 1.0 and c.game_id is not None:
            boosted_by_game.setdefault(c.game_id, []).append(c)

    for gid, players in boosted_by_game.items():
        if len(players) >= BOOST_CONCENTRATION_THRESHOLD:
            warnings.append(
                f"Game {gid} has {len(players)} boosted players. "
                f"Spread boosts across 2-3 favorable games."
            )
            players.sort(key=lambda c: c.filter_ev, reverse=True)
            for p in players[BOOST_CONCENTRATION_THRESHOLD - 1:]:
                p.filter_ev *= BOOST_CONCENTRATION_PENALTY

    return warnings


def _smart_slot_assignment(
    candidates: list[FilteredCandidate],
) -> list[FilterSlotAssignment]:
    """V6.0 slot assignment: pitcher anchors Slot 1, batters fill Slots 2-5.

    Retains V5.0 structure.  The pitcher is pinned to Slot 1 (2.0x).
    Batters are assigned to Slots 2-5 by filter_ev descending (highest
    EV batter gets Slot 2 at 1.8x).
    """
    if not candidates:
        return []

    slot_mults = dict(SLOT_MULTIPLIERS)  # {1: 2.0, 2: 1.8, ...}

    pitcher = next((c for c in candidates if c.is_pitcher), None)
    batters = sorted(
        [c for c in candidates if not c.is_pitcher],
        key=lambda c: c.filter_ev,
        reverse=True,
    )

    assignments: list[FilterSlotAssignment] = []

    # Pitcher → Slot 1 (2.0x multiplier).
    if pitcher is not None:
        anchor_mult = slot_mults[PITCHER_ANCHOR_SLOT]
        slot_value = pitcher.filter_ev * (anchor_mult / BASE_MULTIPLIER)
        assignments.append(FilterSlotAssignment(
            slot_index=PITCHER_ANCHOR_SLOT,
            slot_mult=anchor_mult,
            candidate=pitcher,
            expected_slot_value=round(slot_value, 2),
        ))

    # Batters → remaining slots by filter_ev descending.
    batter_slots = sorted(
        ((idx, mult) for idx, mult in slot_mults.items() if idx != PITCHER_ANCHOR_SLOT),
        key=lambda x: x[1],
        reverse=True,
    )

    for player, (slot_idx, slot_mult) in zip(batters, batter_slots):
        slot_value = player.filter_ev * (slot_mult / BASE_MULTIPLIER)
        assignments.append(FilterSlotAssignment(
            slot_index=slot_idx,
            slot_mult=slot_mult,
            candidate=player,
            expected_slot_value=round(slot_value, 2),
        ))

    assignments.sort(key=lambda a: a.slot_index)
    return assignments


# ---------------------------------------------------------------------------
# Main filter pipeline
# ---------------------------------------------------------------------------

def run_filter_strategy(
    candidates: list[FilteredCandidate],
    slate_classification: SlateClassification,
) -> FilterOptimizedLineup:
    """
    Run the full "Filter, Not Forecast" pipeline.

    This is the main entry point. Takes pre-scored, pre-filtered
    candidates and produces an optimized lineup following all 5 filters.

    Steps:
    1. Compute filter-adjusted EV for each candidate (Filters 2-4)
    2. Enforce composition targets from slate classification (Filter 1)
    3. Check game diversification (Commandment 10)
    4. Smart slot assignment (Filter 5)
    """
    if not candidates:
        return FilterOptimizedLineup(
            slots=[],
            total_expected_value=0.0,
            strategy="filter_not_forecast",
            slate_classification=slate_classification,
        )

    # Mark blowout-game players before EV computation so _compute_filter_ev()
    # can apply the stack_bonus without needing slate_classification as a parameter.
    blowout_teams = {
        g.favored_team.upper()
        for g in slate_classification.stackable_games
        if g.favored_team
    }
    for c in candidates:
        c.is_in_blowout_game = c.team.upper() in blowout_teams

    # Step 1: Compute filter-adjusted EV (popularity + env + trait + context)
    for c in candidates:
        c.filter_ev = _compute_filter_ev(c)

    # Step 2: Enforce composition — V6.0: pure EV ranking with team/game caps.
    lineup = _enforce_composition(candidates, slate_classification)

    # Step 3: Game diversification (safety-net; team/game caps enforced upstream)
    warnings = _apply_game_diversification(lineup)

    # Step 4: Smart slot assignment
    slots = _smart_slot_assignment(lineup)

    total_ev = sum(s.expected_slot_value for s in slots)
    pitcher_count = sum(1 for s in slots if s.candidate.is_pitcher)
    hitter_count = len(slots) - pitcher_count

    return FilterOptimizedLineup(
        slots=slots,
        total_expected_value=round(total_ev, 2),
        strategy="filter_not_forecast",
        slate_classification=slate_classification,
        composition={"pitchers": pitcher_count, "hitters": hitter_count},
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Moonshot — completely different 5, anti-crowd, sharp-signal, explosive
# ---------------------------------------------------------------------------

def _moonshot_popularity_adj(popularity: PopularityClass, is_pitcher: bool = False) -> float:
    """Return Moonshot-specific popularity EV multiplier (heavier lean).

    Pitchers get lighter FADE penalty in Moonshot too (30% vs 40%).
    """
    if popularity == PopularityClass.FADE:
        if is_pitcher:
            return MOONSHOT_PITCHER_FADE_PENALTY
        return MOONSHOT_FADE_PENALTY
    if popularity == PopularityClass.TARGET:
        return MOONSHOT_TARGET_BONUS
    return MOONSHOT_NEUTRAL_PENALTY


def _compute_moonshot_filter_ev(candidate: FilteredCandidate) -> float:
    """Compute Moonshot EV: popularity-first formula + contrarian lean + sharp/explosive.

    V6.0: Uses RS_CONDITION_MATRIX for the base pop_factor, then applies
    additional contrarian multipliers — Moonshot leans even harder into
    crowd-avoidance.
    """
    from app.services.condition_classifier import get_rs_condition_factor
    from app.core.constants import MOONSHOT_CONTRARIAN_FADE_MULT, MOONSHOT_CONTRARIAN_TARGET_MULT

    # Base pop_factor from condition matrix
    pop_factor = get_rs_condition_factor(
        candidate.popularity.value,
        is_pitcher=candidate.is_pitcher,
    )

    # Moonshot contrarian lean: further penalize FADE, reward TARGET
    if candidate.popularity == PopularityClass.FADE:
        pop_factor *= MOONSHOT_CONTRARIAN_FADE_MULT
    elif candidate.popularity == PopularityClass.TARGET:
        pop_factor *= MOONSHOT_CONTRARIAN_TARGET_MULT
    else:
        pop_factor *= MOONSHOT_NEUTRAL_PENALTY

    base_ev = _compute_base_ev(candidate, pop_factor)

    sharp_bonus = 1.0 + (candidate.sharp_score / 100.0) * MOONSHOT_SHARP_BONUS_MAX
    explosive_trait = get_trait_score(
        candidate.traits, "k_rate" if candidate.is_pitcher else "power_profile"
    )
    explosive_bonus = 1.0 + (explosive_trait / 25.0) * MOONSHOT_EXPLOSIVE_BONUS_MAX

    return base_ev * sharp_bonus * explosive_bonus


@dataclass
class DualFilterOptimizedResult:
    starting_5: FilterOptimizedLineup
    moonshot: FilterOptimizedLineup


def run_dual_filter_strategy(
    candidates: list[FilteredCandidate],
    slate_classification: SlateClassification,
) -> DualFilterOptimizedResult:
    """Produce both Starting 5 and Moonshot from the same candidate pool.

    Starting 5: rank by popularity + env + trait EV; pitcher anchors Slot 1.
    Moonshot:   same base EV formula with Moonshot popularity weights (heavier
                FADE penalty, TARGET bonus) plus sharp-signal and explosive
                trait bonuses.  No player overlap with Starting 5; a same-team
                penalty discourages teammate overlap.

    Ownership-based cross-lineup correlation was removed with the drafts
    input — same-team Moonshot penalty + team/game diversification caps
    already produce lineup variance without it.
    """
    # Phase 1: Starting 5
    starting_5 = run_filter_strategy(candidates, slate_classification)

    s5_names = {s.candidate.player_name for s in starting_5.slots}
    s5_teams = {s.candidate.team.upper() for s in starting_5.slots}

    # Phase 2: Moonshot from the remaining pool
    moonshot_pool = [c for c in candidates if c.player_name not in s5_names]

    if not moonshot_pool:
        empty_moonshot = FilterOptimizedLineup(
            slots=[],
            total_expected_value=0.0,
            strategy="moonshot",
            slate_classification=slate_classification,
        )
        return DualFilterOptimizedResult(starting_5=starting_5, moonshot=empty_moonshot)

    for c in moonshot_pool:
        c.filter_ev = _compute_moonshot_filter_ev(c)
        if c.team.upper() in s5_teams:
            c.filter_ev *= MOONSHOT_SAME_TEAM_PENALTY

    moonshot_lineup = _enforce_composition(moonshot_pool, slate_classification)
    moonshot_warnings = _apply_game_diversification(moonshot_lineup)
    moonshot_slots = _smart_slot_assignment(moonshot_lineup)

    moonshot_total_ev = sum(s.expected_slot_value for s in moonshot_slots)
    moonshot_pitcher_count = sum(1 for s in moonshot_slots if s.candidate.is_pitcher)
    moonshot_hitter_count = len(moonshot_slots) - moonshot_pitcher_count

    moonshot = FilterOptimizedLineup(
        slots=moonshot_slots,
        total_expected_value=round(moonshot_total_ev, 2),
        strategy="moonshot",
        slate_classification=slate_classification,
        composition={"pitchers": moonshot_pitcher_count, "hitters": moonshot_hitter_count},
        warnings=moonshot_warnings,
    )

    return DualFilterOptimizedResult(starting_5=starting_5, moonshot=moonshot)
