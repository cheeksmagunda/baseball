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
    BOOST_CONCENTRATION_THRESHOLD,
    BOOST_CONCENTRATION_PENALTY,
    SLOT1_DIFFERENTIATOR_EV_THRESHOLD,
    GHOST_ENFORCE_SWAP_THRESHOLD,
    MAX_PLAYERS_PER_TEAM,
    STACK_BONUS,
)
from app.core.utils import BASE_MULTIPLIER, get_trait_score
from app.services.popularity import PopularityClass

logger = logging.getLogger(__name__)


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

    V2 §3 Slate Classification (revised):
    - Tiny (1-3 games): limited pool, heavy team-stack
    - Pitcher Day (4+ quality SP matchups): go 4-5 pitchers
    - Hitter/Stack Day (4+ high O/U OR 1+ blowout game): stack the favorite
    - Standard: 2-3P + 2-3 hitters

    V2 key insight: "Read the slate, don't default to pitchers."
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

        # Blowout detection (V2 §2 Pillar 2): moneyline ≥ -200 = projected blowout
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

    # Classification logic — V2: check hitter/stack BEFORE pitcher day
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

    # V2: Blowout games trigger hitter/stack day even without high O/U counts
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
) -> tuple[float, list[str]]:
    """
    Compute environmental score for a batter (0-1.0).

    V2 §4 batter filters:
    - Playing in high Vegas total game (O/U >= 8.5)
    - Facing a weak opposing starter (high ERA)
    - Having a platoon advantage
    - Batting in top 5 of lineup (V2 says top 5, not top 4)
    - Hitter-friendly park or favorable weather
    - Team is moneyline favorite (V2 addition)
    - Vulnerable opposing bullpen (high bullpen ERA)
    """
    score = 0.0
    factors = []
    max_score = 7.0  # 7 factors (added bullpen vulnerability)

    # 1. High Vegas total (run environment)
    if vegas_total is not None:
        if vegas_total >= BATTER_ENV_HIGH_VEGAS_TOTAL:
            score += 1.0
            factors.append(f"High-run environment (O/U={vegas_total:.1f})")
        elif vegas_total >= 7.5:
            score += 0.5

    # 2. Weak opposing starter
    if opp_pitcher_era is not None:
        if opp_pitcher_era >= BATTER_ENV_WEAK_PITCHER_ERA:
            score += 1.0
            factors.append(f"Weak opposing starter (ERA={opp_pitcher_era:.2f})")
        elif opp_pitcher_era >= 4.0:
            score += 0.5

    # 3. Platoon advantage
    if platoon_advantage:
        score += 1.0
        factors.append("Platoon advantage")

    # 4. Top of lineup (V2 says top 5)
    if batting_order is not None:
        if batting_order <= BATTER_ENV_TOP_LINEUP:
            score += 1.0
            factors.append(f"Top of lineup (bats #{batting_order})")
        elif batting_order <= 6:
            score += 0.5

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

    # 6. Team is moneyline favorite (V2 §4 batter filter addition)
    if team_moneyline is not None:
        if team_moneyline <= BLOWOUT_MONEYLINE_THRESHOLD:
            score += 1.0
            factors.append(f"Heavy favorite (ML={team_moneyline})")
        elif team_moneyline < -120:
            score += 0.5
            factors.append(f"Moneyline favorite (ML={team_moneyline})")

    # 7. Vulnerable opposing bullpen (high ERA = late-game upside)
    # Starting pitchers only pitch ~5-6 innings; batters get 1-2 PAs against the
    # bullpen in the 7th-9th innings where high-leverage runs are generated.
    # A great starter with a terrible bullpen behind him is still a favorable
    # environment for batters — the crowd only looks at the starter.
    if opp_bullpen_era is not None:
        if opp_bullpen_era >= BATTER_ENV_WEAK_BULLPEN_ERA:
            score += 1.0
            factors.append(f"Vulnerable bullpen (ERA={opp_bullpen_era:.2f})")
        elif opp_bullpen_era >= BATTER_ENV_WEAK_BULLPEN_ERA - 0.5:
            score += 0.5
            factors.append(f"Below-avg bullpen (ERA={opp_bullpen_era:.2f})")

    # Debut/return bonus
    if is_debut_or_return:
        score += 0.5
        factors.append("Debut/return premium")

    env_score = min(1.0, score / max_score)
    return env_score, factors



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
    popularity: PopularityClass = PopularityClass.NEUTRAL  # web-scraped
    is_debut_or_return: bool = False
    game_id: int | str | None = None  # for diversification tracking
    is_pitcher: bool = False
    sharp_score: float = 0.0
    drafts: int | None = None
    is_most_drafted_3x: bool = False  # V2: 57% bust rate trap signal
    traits: list = field(default_factory=list)  # TraitScore list from scoring engine
    is_in_blowout_game: bool = False  # set by run_filter_strategy before EV computation
    total_slate_drafts: int | None = None  # sum of all drafts on the slate (for dynamic thresholds)

    # Computed by the optimizer
    filter_ev: float = 0.0


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


def _popularity_ev_adjustment(popularity: PopularityClass) -> float:
    """Return EV multiplier based on web-scraped popularity classification."""
    if popularity == PopularityClass.FADE:
        return POPULARITY_FADE_PENALTY
    if popularity == PopularityClass.TARGET:
        return POPULARITY_TARGET_BONUS
    return 1.0


def _compute_filter_ev(candidate: FilteredCandidate) -> float:
    """
    Compute composite EV via 4-term condition-based formula.

    Replaces the 15-modifier V2.4 pipeline.  The core insight: ownership × boost
    tier predicts HV rate 4× more reliably than trait scores do.  Starting from
    total_score × (2 + boost) bakes in a chalk bias that all the downstream
    bonuses can't undo.  This formula starts from the historical HV rate instead.

    Formula:
        filter_ev = condition_hv_rate × rs_prob × stack_bonus × anti_crowd × debut_bonus × 100

    The card_boost effect is already captured in TWO places:
      1. condition_hv_rate — ghost+3.0x maps to 1.00 HV rate (boost baked in)
      2. rs_prob — threshold = 15/(2+boost), so higher boost → easier threshold
    Multiplying by (2 + card_boost) again would double-count the boost.
    """
    from app.services.condition_classifier import get_condition_hv_rate
    from app.services.scoring_engine import estimate_rs_probability

    # Term 1: Historical HV rate from condition matrix, blended with ML (primary signal)
    condition_hv_rate = get_condition_hv_rate(
        candidate.drafts, candidate.card_boost,
        is_pitcher=candidate.is_pitcher,
        total_slate_drafts=candidate.total_slate_drafts,
    )

    # Term 2: P(RS >= threshold) where threshold = 15 / (2 + boost)
    rs_prob = estimate_rs_probability(candidate.card_boost, candidate.traits, candidate.is_pitcher)

    # Term 3: Blowout game stack bonus
    stack_bonus = STACK_BONUS if candidate.is_in_blowout_game else 1.0

    # Term 4: Anti-crowd adjustment (popularity signal)
    anti_crowd = _popularity_ev_adjustment(candidate.popularity)

    # Debut/return premium (first appearance → near-zero ownership + historically elite RS)
    debut_bonus = DEBUT_RETURN_EV_BONUS if candidate.is_debut_or_return else 1.0

    effective_score = condition_hv_rate * rs_prob * stack_bonus * anti_crowd * debut_bonus * 100.0
    # The composite score IS the final EV.  Do NOT multiply by (2 + card_boost)
    # again — the condition matrix HV rate and rs_prob already account for boost
    # (ghost+3.0x → 1.00 HV rate precisely because boost is baked into the
    # historical outcome; rs_prob threshold drops with higher boost).
    return effective_score


def _build_team_stack(
    candidates: list[FilteredCandidate],
    stackable_games: list[StackableGame],
) -> list[FilteredCandidate] | None:
    """
    Build a team stack from ghost-ownership players on the favored team.

    V2 §2 Pillar 2: "Stack FROM THE GHOST POOL."
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

        # Sort by filter_ev but prioritize ghost+boost players (V2 ghost-stack principle)
        def stack_sort_key(c: FilteredCandidate) -> tuple:
            is_ghost = c.drafts is not None and c.drafts < LOW_DRAFT_THRESHOLD
            has_boost = c.card_boost >= GHOST_BOOST_SYNERGY_MIN_BOOST
            # Primary: ghost+boost combo, Secondary: ghost, Tertiary: filter_ev
            priority = 0
            if is_ghost and has_boost:
                priority = 2
            elif is_ghost:
                priority = 1
            return (priority, c.filter_ev)

        team_candidates.sort(key=stack_sort_key, reverse=True)
        stack = team_candidates[:STACK_MAX_PLAYERS]

        if len(stack) >= STACK_MIN_PLAYERS:
            ghost_count = sum(
                1 for c in stack
                if c.drafts is not None and c.drafts < GHOST_DRAFT_THRESHOLD
            )
            logger.info(
                "Team stack: %d %s players (ghosts: %d, game_id: %s, ML: %s)",
                len(stack), team, ghost_count, game_id, sg.moneyline,
            )
            return stack

    return None


def _enforce_composition(
    candidates: list[FilteredCandidate],
    slate_class: SlateClassification,
) -> list[FilteredCandidate]:
    """
    AUTO_INCLUDE-first lineup construction. No position forcing. No "day types."

    Historical data (13 rank-1 winners) proves composition varies wildly:
    0P/5H, 1P/4H, 2P/3H, 3P/2H, 4P/1H, 5P/0H — all won on different days.
    The ONLY constant: ghost+boost players (< 100 drafts, boost ≥ 2.5) win at
    82–100% HV rate regardless of position or env score.

    Construction logic:
    1. Separate candidates into AUTO_INCLUDE (ghost+elite boost) and rest.
    2. Prioritize AUTO_INCLUDE: they fill spots before any other candidate is
       considered — ownership × boost tier is the entry criterion, not a modifier.
    3. Backfill from remaining candidates sorted by filter_ev.
    4. If stackable blowout game exists → try team stack + diversifiers (unchanged).
    5. Validate: max 1 mega-chalk, try for ≥1 ghost.

    Position is NEVER forced. EV × condition tier decides everything.
    """
    from app.services.condition_classifier import is_auto_include

    # Sort by filter_ev within each tier
    all_sorted = sorted(candidates, key=lambda c: c.filter_ev, reverse=True)

    # Two-tier ordering: AUTO_INCLUDE candidates always precede regular candidates
    auto = [c for c in all_sorted if is_auto_include(c.drafts, c.card_boost)]
    rest = [c for c in all_sorted if not is_auto_include(c.drafts, c.card_boost)]
    ordered = auto + rest

    # --- Try team stacking when a blowout game exists ---
    if slate_class.stackable_games:
        stack = _build_team_stack(candidates, slate_class.stackable_games)
        if stack and len(stack) >= STACK_MIN_PLAYERS:
            stack_names = {c.player_name for c in stack}
            stack_game_ids = {c.game_id for c in stack}
            diversifiers = [
                c for c in ordered
                if c.player_name not in stack_names
                and c.game_id not in stack_game_ids
            ]
            spots_left = 5 - len(stack)
            lineup = list(stack) + diversifiers[:spots_left]
            lineup = _validate_lineup_structure(lineup, ordered)
            pitcher_count = sum(1 for c in lineup if c.is_pitcher)
            logger.info(
                "Stack construction: %d stack + %d diversifiers = %dP/%dH (auto_include: %d)",
                len(stack), len(lineup) - len(stack),
                pitcher_count, 5 - pitcher_count, len(auto),
            )
            return lineup[:5]

    # --- AUTO_INCLUDE-first EV ranking ---
    lineup = ordered[:5]
    lineup = _validate_lineup_structure(lineup, ordered)
    pitcher_count = sum(1 for c in lineup if c.is_pitcher)
    logger.info(
        "EV-driven composition: %dP/%dH (candidates: %d, auto_include: %d)",
        pitcher_count, 5 - pitcher_count, len(candidates), len(auto),
    )
    return lineup


def _validate_lineup_structure(
    lineup: list[FilteredCandidate],
    all_candidates_sorted: list[FilteredCandidate],
) -> list[FilteredCandidate]:
    """
    V2 §5 Step 6 Final Check — enforce anchor/ghost structure.

    Every rank-1 lineup across 13 days followed this pattern:
    - 1 anchor (consensus play providing a floor)
    - 2-3 differentiators (ghost players ranks 2-8 don't have)
    - 1 flex

    Validation rules:
    - Max 1 mega-chalk (2000+ drafts) player
    - Try to include at least 1 ghost (< 100 drafts) player
    """
    if len(lineup) < 5:
        return lineup

    # Rule 1: Max 1 mega-chalk player
    mega_chalk_indices = [
        i for i, c in enumerate(lineup)
        if c.drafts is not None and c.drafts >= MEGA_CHALK_DRAFT_THRESHOLD
    ]
    if len(mega_chalk_indices) > MAX_MEGA_CHALK_IN_LINEUP:
        # Keep the highest-EV mega-chalk, replace the rest
        mega_chalk_sorted = sorted(mega_chalk_indices, key=lambda i: lineup[i].filter_ev, reverse=True)
        lineup_names = {c.player_name for c in lineup}
        for idx in mega_chalk_sorted[MAX_MEGA_CHALK_IN_LINEUP:]:
            replacement = next(
                (c for c in all_candidates_sorted
                 if c.player_name not in lineup_names
                 and (c.drafts is None or c.drafts < MEGA_CHALK_DRAFT_THRESHOLD)),
                None,
            )
            if replacement:
                lineup_names.discard(lineup[idx].player_name)
                lineup[idx] = replacement
                lineup_names.add(replacement.player_name)

    # Rule 2: Try to include at least 1 ghost player
    ghost_count = sum(
        1 for c in lineup
        if c.drafts is not None and c.drafts < GHOST_DRAFT_THRESHOLD
    )
    if ghost_count < MIN_GHOST_IN_LINEUP:
        lineup_names = {c.player_name for c in lineup}
        # First preference: ghost with full env support.
        # Fallback: mega-ghost+boost even without env — their EV floor already
        # compensates for data-scarcity-driven low env scores (see _apply_ghost_boost_ev_floor).
        best_ghost = next(
            (c for c in all_candidates_sorted
             if c.player_name not in lineup_names
             and c.drafts is not None
             and c.drafts < GHOST_DRAFT_THRESHOLD
             and c.env_score >= ENV_PASS_THRESHOLD),
            None,
        )
        if best_ghost is None:
            # No env-passing ghost — try mega-ghost+boost (env gate waived per V2.4)
            best_ghost = next(
                (c for c in all_candidates_sorted
                 if c.player_name not in lineup_names
                 and c.drafts is not None
                 and c.drafts < MEGA_GHOST_BOOST_MAX_DRAFTS
                 and c.card_boost >= 3.0),
                None,
            )
        if best_ghost:
            # Replace the lowest-EV non-ghost, non-stacked player.
            # A "stacked" player shares a team with 2+ others in the lineup
            # (i.e., part of a 3-player team stack).  Breaking a stack to insert
            # a standalone ghost destroys correlated upside.
            from collections import Counter as _Counter
            _team_counts = _Counter(c.team for c in lineup)
            stacked_teams = {team for team, cnt in _team_counts.items() if cnt >= 3}

            swap_indices = [
                i for i in range(len(lineup))
                if lineup[i].team not in stacked_teams
            ]

            if swap_indices:
                worst_idx = min(swap_indices, key=lambda i: lineup[i].filter_ev)
                if best_ghost.filter_ev >= lineup[worst_idx].filter_ev * GHOST_ENFORCE_SWAP_THRESHOLD:
                    lineup[worst_idx] = best_ghost
            else:
                # All non-ghost players are part of stacks — skip ghost enforcement
                logger.info(
                    "Ghost enforcement skipped: all lineup players are part of team stacks"
                )

    # Rule 3: Max 1 starting pitcher (V2.3)
    # Ghost+boost batter edge outweighs a second SP slot. Excess pitchers are
    # replaced by the highest-EV non-pitchers from the candidate pool.
    pitcher_indices = [i for i, c in enumerate(lineup) if c.is_pitcher]
    if len(pitcher_indices) > MAX_PITCHERS_IN_LINEUP:
        # Keep the single highest-EV pitcher; replace the rest
        pitcher_indices_by_ev = sorted(pitcher_indices, key=lambda i: lineup[i].filter_ev, reverse=True)
        lineup_names = {c.player_name for c in lineup}
        for idx in pitcher_indices_by_ev[MAX_PITCHERS_IN_LINEUP:]:
            replacement = next(
                (c for c in all_candidates_sorted
                 if c.player_name not in lineup_names and not c.is_pitcher),
                None,
            )
            if replacement:
                removed = lineup[idx]
                lineup_names.discard(removed.player_name)
                lineup[idx] = replacement
                lineup_names.add(replacement.player_name)
                logger.info(
                    "Pitcher cap (max %d): replaced pitcher %s (EV=%.2f) with batter %s (EV=%.2f)",
                    MAX_PITCHERS_IN_LINEUP, removed.player_name, removed.filter_ev,
                    replacement.player_name, replacement.filter_ev,
                )

    # Rule 4: Max N players per team (diversification)
    # Prevents over-concentration in a single team's outcome.
    from collections import Counter
    team_counts = Counter(c.team for c in lineup)
    for team, count in team_counts.items():
        if count > MAX_PLAYERS_PER_TEAM:
            # Find indices of players from this team, sorted by EV descending
            team_indices = sorted(
                [i for i, c in enumerate(lineup) if c.team == team],
                key=lambda i: lineup[i].filter_ev,
                reverse=True,
            )
            lineup_names = {c.player_name for c in lineup}
            # Keep the top MAX_PLAYERS_PER_TEAM, replace the rest
            for idx in team_indices[MAX_PLAYERS_PER_TEAM:]:
                # Find best candidate whose team isn't already at the cap
                current_team_counts = Counter(c.team for c in lineup)
                replacement = next(
                    (c for c in all_candidates_sorted
                     if c.player_name not in lineup_names
                     and current_team_counts.get(c.team, 0) < MAX_PLAYERS_PER_TEAM),
                    None,
                )
                if replacement:
                    removed_name = lineup[idx].player_name
                    removed_team = lineup[idx].team
                    lineup_names.discard(removed_name)
                    lineup[idx] = replacement
                    lineup_names.add(replacement.player_name)
                    logger.info(
                        "Team cap (max %d per team): replaced %s (%s) with %s (%s)",
                        MAX_PLAYERS_PER_TEAM, removed_name, removed_team,
                        replacement.player_name, replacement.team,
                    )

    return lineup


def _apply_game_diversification(
    lineup: list[FilteredCandidate],
) -> list[str]:
    """
    Check game diversification (V2 Law 9).

    V2 endorses stacking 3-4 players from the same team/game.
    Only apply soft penalty for 5th player from same game (full concentration).
    Warn if all 5 are in 1 game.
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

    if games_represented < MIN_GAMES_REPRESENTED and len(lineup) >= 5:
        warnings.append(
            f"Only {games_represented} game(s) represented. "
            f"Strategy recommends at least {MIN_GAMES_REPRESENTED}."
        )

    # V2: Only penalize 5th player from same game — stacking 3-4 is correct
    for gid, count in game_counts.items():
        if count >= 5:
            warnings.append(
                f"All 5 players from game {gid}. Consider 1-2 diversifiers."
            )
            # Soft penalty only on the lowest-EV player from the concentrated game
            game_players = sorted(
                [c for c in lineup if c.game_id == gid],
                key=lambda c: c.filter_ev,
            )
            game_players[0].filter_ev *= SAME_GAME_EXCESS_PENALTY

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
    """
    Smart slot assignment (Filter 5 — §4.2 Filter 5).

    Key rules from strategy doc §3.3-§3.4:
    - Unboosted players MUST go in top slots (67% value loss Slot 1→5)
    - Boosted players are slot-flexible (only 16% loss Slot 1→5 with 3.0 boost)
    - Slot 1: highest-conviction play
    - The Slot 1 Differentiator Principle: put contrarian play in Slot 1

    Algorithm:
    1. Sort candidates by filter_ev descending (highest conviction first)
    2. Assign unboosted/low-boost players to highest available slots
    3. Assign high-boost players to remaining slots (they're flexible)
    """
    if not candidates:
        return []

    slot_mults = sorted(SLOT_MULTIPLIERS.items(), key=lambda x: x[1], reverse=True)
    available_slots = list(slot_mults[:5])

    # Separate into boost tiers
    unboosted = [c for c in candidates if c.card_boost < 1.0]
    boosted = [c for c in candidates if c.card_boost >= 1.0]

    # Sort each group by filter_ev descending
    unboosted.sort(key=lambda c: c.filter_ev, reverse=True)
    boosted.sort(key=lambda c: c.filter_ev, reverse=True)

    assignments: list[FilterSlotAssignment] = []

    # Step 1: Assign unboosted players to highest available slots first
    # (they lose the most value in lower slots)
    for player in unboosted:
        if not available_slots:
            break
        slot_idx, slot_mult = available_slots.pop(0)  # take highest available
        # Additive formula: total_value = RS × (slot_mult + card_boost)
        # filter_ev = intrinsic × (BASE_MULTIPLIER + card_boost), reverse to get intrinsic
        intrinsic = player.filter_ev / (BASE_MULTIPLIER + player.card_boost)
        slot_value = intrinsic * (slot_mult + player.card_boost)
        assignments.append(FilterSlotAssignment(
            slot_index=slot_idx,
            slot_mult=slot_mult,
            candidate=player,
            expected_slot_value=round(slot_value, 2),
        ))

    # Step 2: Assign boosted players to remaining slots
    # (they're slot-flexible due to additive formula)
    for player in boosted:
        if not available_slots:
            break
        slot_idx, slot_mult = available_slots.pop(0)
        intrinsic = player.filter_ev / (BASE_MULTIPLIER + player.card_boost)
        slot_value = intrinsic * (slot_mult + player.card_boost)
        assignments.append(FilterSlotAssignment(
            slot_index=slot_idx,
            slot_mult=slot_mult,
            candidate=player,
            expected_slot_value=round(slot_value, 2),
        ))

    # Slot 1 Differentiator Principle (§3.4):
    # If Slot 1 is a high-ownership consensus pick, swap with a contrarian
    # in a lower slot — but only if the EV sacrifice is small.
    if len(assignments) >= 2:
        slot1_assign = next((a for a in assignments if a.slot_index == 1), None)
        if slot1_assign is not None:
            s1 = slot1_assign.candidate
            is_consensus = (
                s1.popularity == PopularityClass.FADE
                or (s1.drafts is not None and s1.drafts >= CHALK_DRAFT_THRESHOLD)
            )
            if is_consensus:
                best_swap = None
                for a in assignments:
                    if a.slot_index == 1:
                        continue
                    c = a.candidate
                    is_contrarian = (
                        c.popularity == PopularityClass.TARGET
                        or (c.drafts is not None and c.drafts < LOW_DRAFT_THRESHOLD)
                    )
                    if is_contrarian and c.card_boost < 1.0:
                        if c.filter_ev >= s1.filter_ev * SLOT1_DIFFERENTIATOR_EV_THRESHOLD:
                            if best_swap is None or c.filter_ev > best_swap.candidate.filter_ev:
                                best_swap = a

                if best_swap is not None:
                    slot1_assign.slot_index, best_swap.slot_index = best_swap.slot_index, slot1_assign.slot_index
                    slot1_assign.slot_mult, best_swap.slot_mult = best_swap.slot_mult, slot1_assign.slot_mult
                    for a in [slot1_assign, best_swap]:
                        intrinsic = a.candidate.filter_ev / (BASE_MULTIPLIER + a.candidate.card_boost)
                        a.expected_slot_value = round(intrinsic * (a.slot_mult + a.candidate.card_boost), 2)

    # Sort by slot index for display
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

    # Compute total slate draft volume for dynamic ownership thresholds.
    # On a 3-game slate draft counts are concentrated; on a 15-game slate they're
    # diluted.  Passing the slate total lets get_ownership_tier() use percentage-
    # based thresholds instead of fixed draft counts.
    total_slate_drafts = sum(c.drafts for c in candidates if c.drafts is not None) or None

    # Mark blowout-game players before EV computation so _compute_filter_ev()
    # can apply the stack_bonus without needing slate_classification as a parameter.
    blowout_teams = {
        g.favored_team.upper()
        for g in slate_classification.stackable_games
        if g.favored_team
    }
    for c in candidates:
        c.is_in_blowout_game = c.team.upper() in blowout_teams
        c.total_slate_drafts = total_slate_drafts

    # Step 1: Compute filter-adjusted EV (4-term condition-based formula)
    for c in candidates:
        c.filter_ev = _compute_filter_ev(c)

    # Step 2: Enforce composition (pitcher/hitter counts)
    lineup = _enforce_composition(candidates, slate_classification)

    # Step 3: Game diversification check
    warnings = _apply_game_diversification(lineup)

    # Step 3b: Boost diversification check (§4.2 Filter 4)
    boost_warnings = _apply_boost_diversification(lineup)
    warnings.extend(boost_warnings)

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

def _moonshot_popularity_adj(popularity: PopularityClass) -> float:
    """Return Moonshot-specific popularity EV multiplier (heavier lean)."""
    if popularity == PopularityClass.FADE:
        return MOONSHOT_FADE_PENALTY
    if popularity == PopularityClass.TARGET:
        return MOONSHOT_TARGET_BONUS
    return MOONSHOT_NEUTRAL_PENALTY


def _compute_moonshot_filter_ev(candidate: FilteredCandidate) -> float:
    """
    Moonshot EV via 4-term condition-based formula with heavier anti-crowd lean.

    Same structure as _compute_filter_ev() but:
    - anti_crowd uses Moonshot weights (FADE=0.60, TARGET=1.30, NEUTRAL=0.95)
    - Sharp signal bonus: underground buzz → up to +25% EV
    - Explosive bonus: power_profile (batters) or k_rate (pitchers) → up to +10% EV
    """
    from app.services.condition_classifier import get_condition_hv_rate
    from app.services.scoring_engine import estimate_rs_probability

    condition_hv_rate = get_condition_hv_rate(
        candidate.drafts, candidate.card_boost,
        is_pitcher=candidate.is_pitcher,
        total_slate_drafts=candidate.total_slate_drafts,
    )
    rs_prob = estimate_rs_probability(candidate.card_boost, candidate.traits, candidate.is_pitcher)
    stack_bonus = STACK_BONUS if candidate.is_in_blowout_game else 1.0
    anti_crowd = _moonshot_popularity_adj(candidate.popularity)
    debut_bonus = DEBUT_RETURN_EV_BONUS if candidate.is_debut_or_return else 1.0

    effective_score = condition_hv_rate * rs_prob * stack_bonus * anti_crowd * debut_bonus * 100.0
    # Same fix as _compute_filter_ev: boost is already in the condition matrix
    # and rs_prob — do not multiply by (2 + card_boost) again.
    base_ev = effective_score

    # Moonshot-specific bonuses (unchanged from V2)
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
    """
    Produce both Starting 5 and Moonshot from the same candidate pool.

    Starting 5: Best filter EV, standard ownership adjustments.
    Moonshot: Completely different 5 players, heavier anti-crowd lean,
              sharp signal boost, explosive trait bonus, game diversification.
    """
    # Phase 1: Starting 5 (standard filter pipeline)
    starting_5 = run_filter_strategy(candidates, slate_classification)

    # Extract Starting 5 player names and teams for exclusion
    s5_names = {s.candidate.player_name for s in starting_5.slots}
    s5_teams = {s.candidate.team for s in starting_5.slots}

    # Phase 2: Moonshot from remaining pool
    moonshot_pool = [c for c in candidates if c.player_name not in s5_names]

    if not moonshot_pool:
        empty_moonshot = FilterOptimizedLineup(
            slots=[],
            total_expected_value=0.0,
            strategy="moonshot",
            slate_classification=slate_classification,
        )
        return DualFilterOptimizedResult(starting_5=starting_5, moonshot=empty_moonshot)

    # Compute moonshot EV for each remaining candidate
    for c in moonshot_pool:
        c.filter_ev = _compute_moonshot_filter_ev(c)

        # Game diversification: soft penalty for same-team overlap with Starting 5
        if c.team in s5_teams:
            c.filter_ev *= MOONSHOT_SAME_TEAM_PENALTY

    # Enforce composition and build moonshot lineup
    moonshot_lineup = _enforce_composition(moonshot_pool, slate_classification)
    moonshot_warnings = _apply_game_diversification(moonshot_lineup)
    moonshot_boost_warnings = _apply_boost_diversification(moonshot_lineup)
    moonshot_warnings.extend(moonshot_boost_warnings)
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
