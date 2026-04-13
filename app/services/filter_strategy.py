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
    MAX_PLAYERS_PER_GAME,
    MAX_OPPONENTS_SAME_GAME,
    STACK_BONUS,
    DNP_RISK_PENALTY,
    DNP_UNKNOWN_PENALTY,
    DNP_GHOST_UNKNOWN_PENALTY,
    ENV_UNKNOWN_COUNT_THRESHOLD,
    BOOST_QUALITY_THRESHOLD,
    BOOSTED_POOL_FULL_THRESHOLD,
    UNBOOSTED_PITCHER_RICH_POOL_PENALTY,
    UNBOOSTED_PITCHER_RICH_POOL_PENALTY_CEIL,
    MAX_PITCHERS_THIN_POOL,
    # V3.2 constants
    CORRELATION_GHOST_MIN_PLAYERS,
    CORRELATION_EV_BONUS,
    CORRELATION_EV_BONUS_3PLUS,
    MOONSHOT_CORRELATION_TEAMMATE_BONUS,
    ENV_TIEBREAKER_BONUS_MAX,
    ENV_TIEBREAKER_HV_THRESHOLD,
    # V3.4 constants
    BOOSTED_PITCHER_CAP_EXPAND_MIN,
    MAX_PITCHERS_BOOSTED_RICH,
    PITCHER_FADE_PENALTY,
    MOONSHOT_PITCHER_FADE_PENALTY,
    DRAFT_SCARCITY_TIEBREAKER_MAX,
    # V3.5 constants
    MOST_DRAFTED_3X_PENALTY,
    MOST_DRAFTED_3X_ENV_PASS_PENALTY,
)
from app.core.utils import BASE_MULTIPLIER, get_trait_score
from app.services.popularity import PopularityClass

logger = logging.getLogger(__name__)


def compute_dynamic_pitcher_cap(candidates: list) -> int:
    """Compute the maximum number of pitchers allowed in a lineup.

    V3.0: Replaces the rigid MAX_PITCHERS_IN_LINEUP = 1 with a knapsack-style
    approach based on "Boost Pool Richness."

    V3.2: When a ghost+boost pitcher exists (drafts < 100, boost >= 2.5),
    raise cap to 2 even in rich batter pools.

    V3.4: Boosted pitcher cap expansion (April 11 post-mortem).
    When 3+ pitchers with boost >= 2.5 exist (ANY ownership tier), cap = 3.
    When 2 boosted pitchers exist, cap = 2 even with rich batter pool.
    April 11: winning lineups had 3 chalk pitchers with 3.0x boost (Suarez,
    Sheehan, Bassitt) — all blocked by the V3.2 ghost-only exemption.
    PITCHER_CONDITION_MATRIX: chalk+max_boost=0.50, mega_chalk+max_boost=0.67.
    These rates justify competing on EV with ghost+boost batters.
    Historical avg: 2.15 pitchers in rank-1 lineups, range 0-5.

    Logic:
    1. Count ALL boosted pitchers (boost >= 2.5, any ownership tier).
    2. 3+ boosted pitchers → cap = 3 (let EV decide composition).
    3. 2 boosted pitchers → cap = 2.
    4. 1 ghost+boost pitcher → cap = 2 (V3.2 preserved).
    5. Rich batter pool + 0 boosted pitchers → cap = 1.
    6. Thin batter pool → cap = 2.

    Returns: 1, 2, or 3 (the dynamic pitcher cap for this slate).
    """
    quality_boosted_batters = sum(
        1 for c in candidates
        if not c.is_pitcher
        and c.card_boost >= BOOST_QUALITY_THRESHOLD
        and c.env_score >= ENV_PASS_THRESHOLD
    )

    # V3.4: Count ALL boosted pitchers (any ownership tier)
    boosted_pitchers = sum(
        1 for c in candidates
        if c.is_pitcher
        and c.card_boost >= GHOST_BOOST_SYNERGY_MIN_BOOST
    )

    # V3.2: Check for ghost+boost pitchers (subset of boosted_pitchers)
    ghost_boost_pitchers = sum(
        1 for c in candidates
        if c.is_pitcher
        and c.drafts is not None
        and c.drafts < GHOST_DRAFT_THRESHOLD
        and c.card_boost >= GHOST_BOOST_SYNERGY_MIN_BOOST
    )

    # V3.4: 3+ boosted pitchers → cap = 3 regardless of batter pool
    if boosted_pitchers >= BOOSTED_PITCHER_CAP_EXPAND_MIN:
        logger.info(
            "V3.4 dynamic pitcher cap: %d (%d boosted pitchers detected — "
            "letting EV decide composition, quality batters: %d)",
            MAX_PITCHERS_BOOSTED_RICH, boosted_pitchers, quality_boosted_batters,
        )
        return MAX_PITCHERS_BOOSTED_RICH  # 3

    if quality_boosted_batters >= BOOSTED_POOL_FULL_THRESHOLD:
        # V3.4: 2 boosted pitchers → cap = 2 even with rich batter pool
        if boosted_pitchers >= 2:
            logger.info(
                "V3.4 dynamic pitcher cap: 2 (rich pool: %d quality batters, "
                "but %d boosted pitcher(s) — expanding cap)",
                quality_boosted_batters, boosted_pitchers,
            )
            return MAX_PITCHERS_THIN_POOL  # 2
        if ghost_boost_pitchers > 0:
            logger.info(
                "V3.2 dynamic pitcher cap: 2 (rich pool: %d quality batters, "
                "but %d ghost+boost pitcher(s) detected — allowing SP slot)",
                quality_boosted_batters, ghost_boost_pitchers,
            )
            return MAX_PITCHERS_THIN_POOL  # 2
        logger.info(
            "V3.0 dynamic pitcher cap: 1 (rich pool: %d quality boosted batters)",
            quality_boosted_batters,
        )
        return MAX_PITCHERS_IN_LINEUP  # 1
    else:
        logger.info(
            "V3.0 dynamic pitcher cap: %d (thin pool: only %d quality boosted batters, need %d)",
            MAX_PITCHERS_THIN_POOL, quality_boosted_batters, BOOSTED_POOL_FULL_THRESHOLD,
        )
        return MAX_PITCHERS_THIN_POOL  # 2


def _identify_correlation_groups(
    candidates: list,
) -> dict[str, list]:
    """Identify teams with multiple ghost players for cross-lineup correlation.

    V3.2: When MAX_PLAYERS_PER_TEAM=1, within-lineup stacking is impossible.
    Instead, we identify teams with 2+ ghost players and distribute them across
    Starting 5 and Moonshot lineups.  Both lineups gain correlated exposure
    to the same game environment.

    Returns: dict mapping team name → list of ghost candidates on that team
    (only for teams meeting the CORRELATION_GHOST_MIN_PLAYERS threshold).
    """
    from collections import defaultdict

    team_ghosts: dict[str, list] = defaultdict(list)
    for c in candidates:
        if c.is_ghost:
            team_ghosts[c.team.upper()].append(c)

    # Filter to teams meeting the minimum ghost player threshold
    return {
        team: players
        for team, players in team_ghosts.items()
        if len(players) >= CORRELATION_GHOST_MIN_PLAYERS
    }


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
    env_unknown_count: int = 0  # V3.0: how many factors were missing (unknown vs bad)

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

    V3.0: Returns a third value, `unknown_count`, tracking how many environmental
    factors were missing (None) vs. confirmed bad.  This enables the pipeline to
    distinguish "data scarcity" from "genuinely bad conditions" — critical for
    ghost-tier players where missing data is expected, not a negative signal.

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
    unknown_count = 0  # V3.0: track missing data factors
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

    # 4. Top of lineup (V2 says top 5)
    # V3.0: Missing batting order is tracked as unknown, not penalized as bad.
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

    # 6. Team is moneyline favorite (V2 §4 batter filter addition)
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
        factors.append(f"V3.0: {unknown_count} unknown factor(s) (data scarcity, not bad env)")

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
    env_unknown_count: int = 0  # V3.0: how many env factors were missing data
    popularity: PopularityClass = PopularityClass.NEUTRAL  # web-scraped
    is_debut_or_return: bool = False
    game_id: int | str | None = None  # for diversification tracking
    is_pitcher: bool = False
    sharp_score: float = 0.0
    drafts: int | None = None
    is_most_drafted_3x: bool = False  # V2: 57% bust rate trap signal
    traits: list = field(default_factory=list)  # TraitScore list from scoring engine
    batting_order: int | None = None  # 1-9 if confirmed in lineup, None = DNP risk (V2.5)
    is_in_blowout_game: bool = False  # set by run_filter_strategy before EV computation
    total_slate_drafts: int | None = None  # sum of all drafts on the slate (for dynamic thresholds)
    correlation_bonus: float = 1.0  # V3.2: cross-lineup correlation multiplier (set pre-EV)

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
    """Return EV multiplier based on web-scraped popularity classification.

    V3.4: Pitchers get a lighter FADE penalty (15% vs 25%).  Pitchers control
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
    """Compute bifurcated DNP risk adjustment (V3.0).

    Separates "confirmed bad" (lineup published, player absent) from "unknown"
    (data not yet available).  Ghost players missing batting order face data
    scarcity, not a genuinely bad matchup — penalizing them at the same rate
    as chalk with published lineups is asymmetrically wrong for high-boost
    convex payouts.
    """
    if candidate.is_pitcher or candidate.batting_order is not None:
        return 1.0
    if candidate.is_ghost:
        return DNP_GHOST_UNKNOWN_PENALTY
    if candidate.env_unknown_count >= ENV_UNKNOWN_COUNT_THRESHOLD:
        return DNP_UNKNOWN_PENALTY
    return DNP_RISK_PENALTY


def _apply_unboosted_pitcher_penalty(
    candidates: list[FilteredCandidate],
    log_prefix: str = "",
) -> None:
    """Apply env-scaled penalty to unboosted pitchers when the boosted pool is rich.

    V3.1: Penalty scales inversely by env_score.  An ace with env=1.0 gets only
    10% haircut, while a mediocre pitcher with env=0.0 gets the full 35%.

    Shared by run_filter_strategy (Starting 5) and run_dual_filter_strategy (Moonshot).
    """
    quality_boosted_count = sum(
        1 for c in candidates
        if c.card_boost >= BOOST_QUALITY_THRESHOLD
        and c.env_score >= ENV_PASS_THRESHOLD
        and not c.is_pitcher
    )
    if quality_boosted_count < BOOSTED_POOL_FULL_THRESHOLD:
        return

    for c in candidates:
        if c.is_pitcher and c.card_boost < BOOST_QUALITY_THRESHOLD:
            env_adj = min(1.0, max(0.0, c.env_score))
            penalty = (
                UNBOOSTED_PITCHER_RICH_POOL_PENALTY
                + (UNBOOSTED_PITCHER_RICH_POOL_PENALTY_CEIL - UNBOOSTED_PITCHER_RICH_POOL_PENALTY)
                * env_adj
            )
            old_ev = c.filter_ev
            c.filter_ev *= penalty
            logger.debug(
                "Unboosted pitcher penalty (%srich pool, V3.1 env-scaled): "
                "%s env=%.2f penalty=%.2f EV %.2f → %.2f",
                log_prefix, c.player_name, c.env_score, penalty, old_ev, c.filter_ev,
            )


def _compute_base_ev(
    candidate: FilteredCandidate,
    anti_crowd: float,
) -> float:
    """Compute the shared base EV used by both Starting 5 and Moonshot.

    This is the single source of truth for the 4-term condition-based formula.
    Both _compute_filter_ev() and _compute_moonshot_filter_ev() delegate here,
    differing only in the anti_crowd multiplier and moonshot-specific bonuses.

    Formula:
        base_ev = condition_hv_rate × rs_prob × stack_bonus × anti_crowd
                  × debut_bonus × dnp_adj × env_tiebreaker × 100

    The card_boost effect is already captured in TWO places:
      1. condition_hv_rate — ghost+3.0x maps to 1.00 HV rate (boost baked in)
      2. rs_prob — threshold = 15/(2+boost), so higher boost → easier threshold
    Do NOT multiply by (2 + card_boost) again — that would double-count.
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
    debut_bonus = DEBUT_RETURN_EV_BONUS if candidate.is_debut_or_return else 1.0
    dnp_adj = _compute_dnp_adjustment(candidate)

    effective_score = condition_hv_rate * rs_prob * stack_bonus * anti_crowd * debut_bonus * dnp_adj * 100.0

    # V3.2: Environmental tiebreaker for auto-include tier.
    # All ghost+max_boost candidates have condition_hv_rate=1.00, making them
    # indistinguishable by the primary signal.  env_score differentiates:
    # a ghost+max batting 3rd at Coors > one with unknown order at Petco.
    if condition_hv_rate >= ENV_TIEBREAKER_HV_THRESHOLD:
        env_tiebreaker = 1.0 + candidate.env_score * ENV_TIEBREAKER_BONUS_MAX
        effective_score *= env_tiebreaker

        # V3.4: Draft scarcity tiebreaker — within auto-include tier, fewer
        # drafts = deeper crowd asymmetry = higher edge.  A player with 1 draft
        # is more "unknown" than one with 15 — the crowd has priced in more
        # information about the higher-draft player.
        #
        # April 11: optimizer picked 9-15 draft ghosts (García, Dingler,
        # Ballesteros, Valenzuela — all busted) while 1-4 draft ghosts
        # (Moniak RS 6.5, Laureano RS 6.1, Greene RS 5.8, Crawford RS 5.4)
        # were the actual winners.
        #
        # Uses log scale: 1 draft → +10%, 5 → +6.5%, 15 → +4.1%, 50 → +1.5%
        import math
        if (
            candidate.drafts is not None
            and candidate.drafts < GHOST_DRAFT_THRESHOLD
            and GHOST_DRAFT_THRESHOLD > 1
        ):
            scarcity = math.log(GHOST_DRAFT_THRESHOLD / max(1, candidate.drafts)) / math.log(GHOST_DRAFT_THRESHOLD)
            draft_tiebreaker = 1.0 + scarcity * DRAFT_SCARCITY_TIEBREAKER_MAX
            effective_score *= draft_tiebreaker

    return effective_score


def _compute_filter_ev(candidate: FilteredCandidate) -> float:
    """Compute Starting 5 EV via the shared 4-term condition-based formula.

    V3.5: Applies is_most_drafted_3x trap penalty (V2.3 spec, previously
    dead code — flag was computed but never read).  57% bust rate, avg RS 0.72.
    Env-aware: lighter penalty when environmental support exists.
    """
    ev = _compute_base_ev(candidate, _popularity_ev_adjustment(candidate.popularity, is_pitcher=candidate.is_pitcher))
    if candidate.is_most_drafted_3x:
        if candidate.env_score >= ENV_PASS_THRESHOLD:
            ev *= MOST_DRAFTED_3X_ENV_PASS_PENALTY
        else:
            ev *= MOST_DRAFTED_3X_PENALTY
        logger.debug(
            "Most-drafted-3x penalty (S5): %s env=%.2f penalty=%.2f",
            candidate.player_name, candidate.env_score,
            MOST_DRAFTED_3X_ENV_PASS_PENALTY if candidate.env_score >= ENV_PASS_THRESHOLD else MOST_DRAFTED_3X_PENALTY,
        )
    return ev


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
    pitcher_cap: int = MAX_PITCHERS_IN_LINEUP,
) -> list[FilteredCandidate]:
    """
    Three-tier lineup construction: auto → soft_auto → rest.

    V3.2: Added soft_auto_include tier for ghost+mid_boost players (boost 2.0-2.5).
    These have 0.75 HV rate — excellent, but not the 0.88-1.00 of full auto_include.
    They rank after auto-includes but before all non-ghost candidates.

    V3.2: Within-lineup stacking disabled (MAX_PLAYERS_PER_TEAM=1).
    Correlation value captured cross-lineup via run_dual_filter_strategy.

    V3.0: pitcher_cap is now dynamic (1 for rich boosted pools, 2 for thin).

    Construction logic:
    1. Separate candidates into AUTO_INCLUDE, SOFT_AUTO_INCLUDE, and rest.
    2. Three-tier ordering: auto first, then soft_auto, then rest by filter_ev.
    3. Backfill respecting pitcher cap, team cap (1), and game cap (2).
    4. If stackable blowout game exists AND MAX_PLAYERS_PER_TEAM >= 3 → try stack.
    5. Validate: max 1 mega-chalk, try for ≥1 ghost.

    Position is NEVER forced. EV × condition tier decides everything.
    """
    from app.services.condition_classifier import is_auto_include, is_soft_auto_include

    # Sort by filter_ev within each tier
    all_sorted = sorted(candidates, key=lambda c: c.filter_ev, reverse=True)

    # V3.2: Three-tier ordering: auto → soft_auto → rest
    # V3.5: Pass total_slate_drafts so ownership tier classification matches
    # the EV computation path (get_condition_hv_rate also receives it).
    # Without this, is_auto_include uses absolute thresholds (<100=ghost)
    # while the EV formula uses percentage-based thresholds — a player
    # with 120 drafts could get ghost HV rate (1.00) but NOT be treated
    # as auto-include, or vice versa.
    auto = [c for c in all_sorted if is_auto_include(c.drafts, c.card_boost, c.total_slate_drafts)]
    soft_auto = [c for c in all_sorted if is_soft_auto_include(c.drafts, c.card_boost, c.total_slate_drafts)]
    rest = [c for c in all_sorted
            if not is_auto_include(c.drafts, c.card_boost, c.total_slate_drafts)
            and not is_soft_auto_include(c.drafts, c.card_boost, c.total_slate_drafts)]
    ordered = auto + soft_auto + rest

    if soft_auto:
        logger.info(
            "V3.2 three-tier: %d auto-include, %d soft-auto-include, %d rest",
            len(auto), len(soft_auto), len(rest),
        )

    # --- Try team stacking when a blowout game exists ---
    # V3.2: Stacking requires MAX_PLAYERS_PER_TEAM >= STACK_MIN_PLAYERS.
    # With MAX_PLAYERS_PER_TEAM=1, this path is effectively disabled.
    # Correlation value is now captured cross-lineup instead.
    if (
        slate_class.stackable_games
        and MAX_PLAYERS_PER_TEAM >= STACK_MIN_PLAYERS
    ):
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
            stack_pitcher_count = sum(1 for c in stack if c.is_pitcher)
            pitchers_allowed = max(0, pitcher_cap - stack_pitcher_count)
            div_pitchers = 0
            filtered_diversifiers: list[FilteredCandidate] = []
            for c in diversifiers:
                if len(filtered_diversifiers) == spots_left:
                    break
                if c.is_pitcher:
                    if div_pitchers >= pitchers_allowed:
                        continue
                    div_pitchers += 1
                filtered_diversifiers.append(c)
            lineup = list(stack) + filtered_diversifiers
            lineup = _validate_lineup_structure(lineup, ordered, pitcher_cap=pitcher_cap)
            pitcher_count = sum(1 for c in lineup if c.is_pitcher)
            logger.info(
                "Stack construction: %d stack + %d diversifiers = %dP/%dH (auto_include: %d, pitcher_cap: %d)",
                len(stack), len(lineup) - len(stack),
                pitcher_count, 5 - pitcher_count, len(auto), pitcher_cap,
            )
            return lineup[:5]

    # --- Three-tier EV ranking ---
    # Enforce pitcher cap, team cap (MAX_PLAYERS_PER_TEAM), and game cap during selection.
    pitchers_added = 0
    team_used: dict[str, int] = {}  # V3.2: track team → count for MAX_PLAYERS_PER_TEAM=1
    # Track (game_id, team) → count for game-level diversification
    game_team_used: dict[tuple, int] = {}
    # Track game_id → set of teams for opponent detection
    game_teams: dict[int | str | None, set[str]] = {}
    lineup = []
    for c in ordered:
        if len(lineup) == 5:
            break
        if c.is_pitcher:
            if pitchers_added >= pitcher_cap:
                continue
            pitchers_added += 1
        # V3.2: Team cap (MAX_PLAYERS_PER_TEAM=1)
        team_key = c.team.upper()
        if team_used.get(team_key, 0) >= MAX_PLAYERS_PER_TEAM:
            continue
        # Game-level diversification
        if c.game_id is not None:
            gt_key = (c.game_id, team_key)
            current_teammates = game_team_used.get(gt_key, 0)
            if current_teammates >= MAX_PLAYERS_PER_GAME:
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
            game_team_used[gt_key] = current_teammates + 1
            game_teams.setdefault(c.game_id, set()).add(team_key)
        team_used[team_key] = team_used.get(team_key, 0) + 1
        lineup.append(c)
    lineup = _validate_lineup_structure(lineup, ordered, pitcher_cap=pitcher_cap)
    pitcher_count = sum(1 for c in lineup if c.is_pitcher)
    logger.info(
        "EV-driven composition: %dP/%dH (candidates: %d, auto: %d, soft_auto: %d, pitcher_cap: %d)",
        pitcher_count, 5 - pitcher_count, len(candidates), len(auto), len(soft_auto), pitcher_cap,
    )
    return lineup


def _validate_lineup_structure(
    lineup: list[FilteredCandidate],
    all_candidates_sorted: list[FilteredCandidate],
    pitcher_cap: int = MAX_PITCHERS_IN_LINEUP,
) -> list[FilteredCandidate]:
    """
    V2 §5 Step 6 Final Check — enforce anchor/ghost structure.

    Every rank-1 lineup across 13 days followed this pattern:
    - 1 anchor (consensus play providing a floor)
    - 2-3 differentiators (ghost players ranks 2-8 don't have)
    - 1 flex

    V3.0: pitcher_cap is now dynamic (1 for rich pools, 2 for thin).

    Validation rules:
    - Max 1 mega-chalk (2000+ drafts) player
    - Try to include at least 1 ghost (< 100 drafts) player
    - Max pitcher_cap starting pitchers
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
        # Respect pitcher cap: only allow a ghost pitcher if the lineup has room
        pitcher_count_now = sum(1 for c in lineup if c.is_pitcher)
        can_add_pitcher = pitcher_count_now < pitcher_cap

        # Identify stacked teams (teams with 3+ players) whose members are protected
        # from ghost-enforcement swaps (breaking a stack destroys correlated upside).
        from collections import Counter as _Counter
        _team_counts = _Counter(c.team for c in lineup)
        _game_counts = _Counter(c.game_id for c in lineup if c.game_id is not None)
        stacked_teams = {team for team, cnt in _team_counts.items() if cnt >= 3}

        # Determine the swap target FIRST so ghost selection can account for which
        # player is being removed.  This prevents the enforced ghost from being
        # immediately evicted by Rule 3 (team cap) or Rule 3b (game cap) because
        # it shares a team/game with a player that was still in the lineup when
        # the ghost was chosen under the old ordering.
        swap_indices = [
            i for i in range(len(lineup))
            if lineup[i].team not in stacked_teams
        ]

        if not swap_indices:
            # All non-ghost players are part of stacks — skip ghost enforcement
            logger.info(
                "Ghost enforcement skipped: all lineup players are part of team stacks"
            )
        else:
            worst_idx = min(swap_indices, key=lambda i: lineup[i].filter_ev)
            removing = lineup[worst_idx]

            # _ghost_ok checks basic eligibility AND verifies the candidate won't
            # violate team/game caps in the post-swap lineup.
            # Net logic: subtract the removed player's slot, add the ghost's slot.
            def _ghost_ok(c: FilteredCandidate) -> bool:
                if c.player_name in lineup_names:
                    return False
                if c.drafts is None or c.drafts >= GHOST_DRAFT_THRESHOLD:
                    return False
                if c.is_pitcher and not can_add_pitcher:
                    return False
                # Team cap after swap (remove `removing`, add `c`)
                net_team = (
                    _team_counts.get(c.team, 0)
                    - (1 if removing.team == c.team else 0)
                    + 1
                )
                if net_team > MAX_PLAYERS_PER_TEAM:
                    return False
                # Game cap after swap
                if c.game_id is not None:
                    net_game = (
                        _game_counts.get(c.game_id, 0)
                        - (1 if removing.game_id == c.game_id else 0)
                        + 1
                    )
                    if net_game > MAX_PLAYERS_PER_GAME:
                        return False
                return True

            # First preference: ghost with full env support.
            # Fallback: mega-ghost+boost even without env — their EV floor already
            # compensates for data-scarcity-driven low env scores (see _apply_ghost_boost_ev_floor).
            best_ghost = next(
                (c for c in all_candidates_sorted
                 if _ghost_ok(c) and c.env_score >= ENV_PASS_THRESHOLD),
                None,
            )
            if best_ghost is None:
                # No env-passing ghost — try mega-ghost+boost (env gate waived per V2.4)
                best_ghost = next(
                    (c for c in all_candidates_sorted
                     if _ghost_ok(c)
                     and c.drafts is not None
                     and c.drafts < MEGA_GHOST_BOOST_MAX_DRAFTS
                     and c.card_boost >= 3.0),
                    None,
                )
            if best_ghost and best_ghost.filter_ev >= removing.filter_ev * GHOST_ENFORCE_SWAP_THRESHOLD:
                lineup[worst_idx] = best_ghost

    # Rule 3: Max N players per team (diversification)
    # Prevents over-concentration in a single team's outcome.
    # Runs BEFORE pitcher cap so that replacement picks respect the cap below.
    from collections import Counter
    team_counts = Counter(c.team for c in lineup)
    pitcher_count_pre = sum(1 for c in lineup if c.is_pitcher)
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
                # Find best candidate whose team isn't at cap AND won't violate pitcher cap
                current_team_counts = Counter(c.team for c in lineup)
                current_pitcher_count = sum(1 for c in lineup if c.is_pitcher)
                # If we're removing a pitcher, a pitcher replacement is fine.
                # If we're removing a batter, only allow a batter replacement.
                removing_pitcher = lineup[idx].is_pitcher
                # Ghost preservation: if removing the last ghost, replacement must
                # also be a ghost so we don't drop below MIN_GHOST_IN_LINEUP.
                current_ghost_count = sum(
                    1 for c in lineup
                    if c.drafts is not None and c.drafts < GHOST_DRAFT_THRESHOLD
                )
                removing_ghost = (
                    lineup[idx].drafts is not None
                    and lineup[idx].drafts < GHOST_DRAFT_THRESHOLD
                )
                must_replace_with_ghost = (
                    removing_ghost and current_ghost_count <= MIN_GHOST_IN_LINEUP
                )
                replacement = next(
                    (c for c in all_candidates_sorted
                     if c.player_name not in lineup_names
                     and current_team_counts.get(c.team, 0) < MAX_PLAYERS_PER_TEAM
                     and (not must_replace_with_ghost
                          or (c.drafts is not None and c.drafts < GHOST_DRAFT_THRESHOLD))
                     and (not c.is_pitcher or removing_pitcher
                          or current_pitcher_count < pitcher_cap)),
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

    # Rule 3b: Max N players per game/matchup (V2.5 game diversification)
    # Prevents over-concentration in a single game's outcome.
    # Two players in the same game = 40% of lineup on one game.
    from collections import Counter as _GameCounter
    game_counts = _GameCounter(c.game_id for c in lineup if c.game_id is not None)
    for game_id, count in game_counts.items():
        if count > MAX_PLAYERS_PER_GAME:
            # Find indices of players in this game, sorted by EV descending
            game_indices = sorted(
                [i for i, c in enumerate(lineup) if c.game_id == game_id],
                key=lambda i: lineup[i].filter_ev,
                reverse=True,
            )
            lineup_names = {c.player_name for c in lineup}
            # Keep the top MAX_PLAYERS_PER_GAME, replace the rest
            for idx in game_indices[MAX_PLAYERS_PER_GAME:]:
                current_game_counts = _GameCounter(
                    c.game_id for c in lineup if c.game_id is not None
                )
                current_pitcher_count = sum(1 for c in lineup if c.is_pitcher)
                removing_pitcher = lineup[idx].is_pitcher
                # Ghost preservation: same logic as Rule 3.
                current_ghost_count = sum(
                    1 for c in lineup
                    if c.drafts is not None and c.drafts < GHOST_DRAFT_THRESHOLD
                )
                removing_ghost = (
                    lineup[idx].drafts is not None
                    and lineup[idx].drafts < GHOST_DRAFT_THRESHOLD
                )
                must_replace_with_ghost = (
                    removing_ghost and current_ghost_count <= MIN_GHOST_IN_LINEUP
                )
                replacement = next(
                    (c for c in all_candidates_sorted
                     if c.player_name not in lineup_names
                     and (c.game_id is None
                          or current_game_counts.get(c.game_id, 0) < MAX_PLAYERS_PER_GAME)
                     and (not must_replace_with_ghost
                          or (c.drafts is not None and c.drafts < GHOST_DRAFT_THRESHOLD))
                     and (not c.is_pitcher or removing_pitcher
                          or current_pitcher_count < pitcher_cap)),
                    None,
                )
                if replacement:
                    removed_name = lineup[idx].player_name
                    removed_game = lineup[idx].game_id
                    lineup_names.discard(removed_name)
                    lineup[idx] = replacement
                    lineup_names.add(replacement.player_name)
                    logger.info(
                        "Game cap (max %d per game): replaced %s (game=%s) with %s (game=%s)",
                        MAX_PLAYERS_PER_GAME, removed_name, removed_game,
                        replacement.player_name, replacement.game_id,
                    )

    # Rule 4: Max pitcher_cap starting pitchers (V3.0 dynamic, V2.3 was hard 1) — FINAL SWEEP
    # This runs LAST so no subsequent rule can reintroduce pitchers.
    # When boosted pool is rich, cap=1 (ghost+boost batter edge dominates).
    # When thin, cap=2 (unboosted pitchers are the best alternative).
    pitcher_indices = [i for i, c in enumerate(lineup) if c.is_pitcher]
    if len(pitcher_indices) > pitcher_cap:
        # Keep the top pitcher_cap highest-EV pitchers; replace the rest
        pitcher_indices_by_ev = sorted(pitcher_indices, key=lambda i: lineup[i].filter_ev, reverse=True)
        lineup_names = {c.player_name for c in lineup}
        for idx in pitcher_indices_by_ev[pitcher_cap:]:
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
                    pitcher_cap, removed.player_name, removed.filter_ev,
                    replacement.player_name, replacement.filter_ev,
                )
            else:
                # Hard enforcement: remove the pitcher even if no replacement found.
                # A 4-player lineup is better than violating the pitcher cap.
                removed = lineup.pop(idx)
                # Adjust indices for remaining removals
                pitcher_indices_by_ev = [
                    (j if j < idx else j - 1) for j in pitcher_indices_by_ev
                ]
                logger.warning(
                    "Pitcher cap: removed pitcher %s (EV=%.2f) with no batter replacement — "
                    "lineup reduced to %d players",
                    removed.player_name, removed.filter_ev, len(lineup),
                )

    return lineup


def _apply_game_diversification(
    lineup: list[FilteredCandidate],
) -> list[str]:
    """
    Check game diversification (V2.5).

    V2.5: max 1 player per game enforced during composition and validation.
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

    # V3.0: Compute slate draft distribution for percentile-based ownership tiers.
    # The full distribution enables empirical CDF classification instead of
    # arbitrary absolute thresholds.  Also compute meta-game health metrics.
    draft_counts = [c.drafts for c in candidates if c.drafts is not None]
    total_slate_drafts = sum(draft_counts) if draft_counts else None

    # V3.0: Meta-game monitoring — log distribution health metrics.
    # Sustained entropy increase over consecutive slates = ghost edge compression.
    if draft_counts:
        from app.services.condition_classifier import compute_draft_entropy, compute_gini_coefficient
        entropy = compute_draft_entropy(draft_counts)
        gini = compute_gini_coefficient(draft_counts)
        logger.info(
            "V3.0 meta-game monitor: entropy=%.3f bits, gini=%.3f, "
            "slate_players=%d, total_drafts=%s",
            entropy, gini, len(draft_counts), total_slate_drafts,
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
        c.total_slate_drafts = total_slate_drafts

    # Step 1: Compute filter-adjusted EV (4-term condition-based formula)
    for c in candidates:
        c.filter_ev = _compute_filter_ev(c)

    # Step 1a (V3.2): Apply cross-lineup correlation bonus.
    # Candidates on teams with 2+ ghost players get a modest EV lift,
    # increasing their probability of selection in both lineups.
    # The correlation_bonus field is set by run_dual_filter_strategy.
    for c in candidates:
        if c.correlation_bonus != 1.0:
            c.filter_ev *= c.correlation_bonus

    # V3.0: Compute dynamic pitcher cap before composition enforcement
    dynamic_pitcher_cap = compute_dynamic_pitcher_cap(candidates)

    # Step 1b: Unboosted pitcher penalty when boosted pool is rich (V2 §4.3).
    _apply_unboosted_pitcher_penalty(candidates, log_prefix="S5 ")

    # Step 2: Enforce composition (pitcher/hitter counts) with dynamic pitcher cap
    lineup = _enforce_composition(candidates, slate_classification, pitcher_cap=dynamic_pitcher_cap)

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

def _moonshot_popularity_adj(popularity: PopularityClass, is_pitcher: bool = False) -> float:
    """Return Moonshot-specific popularity EV multiplier (heavier lean).

    V3.4: Pitchers get lighter FADE penalty in Moonshot too (30% vs 40%).
    """
    if popularity == PopularityClass.FADE:
        if is_pitcher:
            return MOONSHOT_PITCHER_FADE_PENALTY
        return MOONSHOT_FADE_PENALTY
    if popularity == PopularityClass.TARGET:
        return MOONSHOT_TARGET_BONUS
    return MOONSHOT_NEUTRAL_PENALTY


def _compute_moonshot_filter_ev(candidate: FilteredCandidate) -> float:
    """Compute Moonshot EV: shared base formula + sharp/explosive bonuses.

    Delegates to _compute_base_ev() for the 4-term formula (DRY),
    then applies moonshot-specific sharp signal and explosive trait bonuses.

    V3.5: Applies is_most_drafted_3x penalty — always full 40% for Moonshot
    (max contrarian stance, no env leniency).
    """
    base_ev = _compute_base_ev(candidate, _moonshot_popularity_adj(candidate.popularity, is_pitcher=candidate.is_pitcher))
    if candidate.is_most_drafted_3x:
        base_ev *= MOST_DRAFTED_3X_PENALTY
        logger.debug(
            "Most-drafted-3x penalty (Moonshot): %s penalty=%.2f",
            candidate.player_name, MOST_DRAFTED_3X_PENALTY,
        )

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

    V3.2: Cross-lineup correlation awareness.  With MAX_PLAYERS_PER_TEAM=1,
    within-lineup stacking is impossible.  Instead, when a team has 2+ ghost
    players, the system:
    1. Applies a correlation EV bonus to all ghost players on that team
       (boosting their selection probability in both lineups).
    2. For Moonshot: flips the same-team penalty to a correlation BONUS when
       the Starting 5 already has a teammate from a correlation team.
    This ensures both lineups have exposure to correlated game outcomes.

    Starting 5: Best filter EV, standard ownership adjustments.
    Moonshot: Completely different 5 players, heavier anti-crowd lean,
              sharp signal boost, explosive trait bonus, correlation bonuses.
    """
    # V3.2: Identify correlation groups and set correlation_bonus on candidates
    # BEFORE building either lineup.  run_filter_strategy reads correlation_bonus
    # from each candidate and applies it after computing base EV.
    correlation_teams = _identify_correlation_groups(candidates)
    correlation_boosted: set[str] = set()
    if correlation_teams:
        corr_summary = {t: len(ps) for t, ps in correlation_teams.items()}
        logger.info("V3.2 correlation groups: %s", corr_summary)

        for team, ghost_players in correlation_teams.items():
            bonus = CORRELATION_EV_BONUS_3PLUS if len(ghost_players) >= 3 else CORRELATION_EV_BONUS
            for c in ghost_players:
                c.correlation_bonus = bonus
                correlation_boosted.add(c.player_name)
                logger.debug(
                    "V3.2 correlation bonus: %s (%s) → %.0f%% EV boost (%d ghost teammates)",
                    c.player_name, team, (bonus - 1.0) * 100, len(ghost_players),
                )

    # Phase 1: Starting 5 (standard filter pipeline)
    # correlation_bonus is applied inside run_filter_strategy after base EV computation.
    starting_5 = run_filter_strategy(candidates, slate_classification)

    # Extract Starting 5 player names and teams for exclusion/correlation
    s5_names = {s.candidate.player_name for s in starting_5.slots}
    s5_teams = {s.candidate.team.upper() for s in starting_5.slots}

    # V3.2: Identify which correlation teams have a player in Starting 5
    # (these teams' remaining ghosts should get a BONUS in Moonshot, not a penalty)
    s5_correlation_teams = {
        team for team in correlation_teams
        if team in s5_teams
    }

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

        # V3.2: Cross-lineup correlation logic replaces blanket same-team penalty.
        # If this candidate is a ghost teammate on a correlation team that's already
        # in Starting 5, they get a BONUS (correlated upside across both lineups).
        # Otherwise, the standard same-team penalty applies.
        if c.team.upper() in s5_correlation_teams and c.player_name in correlation_boosted:
            c.filter_ev *= MOONSHOT_CORRELATION_TEAMMATE_BONUS
            logger.debug(
                "V3.2 moonshot correlation bonus: %s (%s) gets +%.0f%% "
                "(teammate in Starting 5, correlated upside)",
                c.player_name, c.team,
                (MOONSHOT_CORRELATION_TEAMMATE_BONUS - 1.0) * 100,
            )
        elif c.team.upper() in s5_teams:
            # Standard same-team penalty for non-correlation overlaps
            c.filter_ev *= MOONSHOT_SAME_TEAM_PENALTY

    # Unboosted pitcher penalty for moonshot too (shared helper, same as Starting 5)
    _apply_unboosted_pitcher_penalty(moonshot_pool, log_prefix="Moonshot ")

    # V3.0: Dynamic pitcher cap for moonshot pool (independent of Starting 5)
    moonshot_pitcher_cap = compute_dynamic_pitcher_cap(moonshot_pool)

    # Enforce composition and build moonshot lineup
    moonshot_lineup = _enforce_composition(moonshot_pool, slate_classification, pitcher_cap=moonshot_pitcher_cap)
    moonshot_warnings = _apply_game_diversification(moonshot_lineup)
    moonshot_boost_warnings = _apply_boost_diversification(moonshot_lineup)
    moonshot_warnings.extend(moonshot_boost_warnings)
    moonshot_slots = _smart_slot_assignment(moonshot_lineup)

    moonshot_total_ev = sum(s.expected_slot_value for s in moonshot_slots)
    moonshot_pitcher_count = sum(1 for s in moonshot_slots if s.candidate.is_pitcher)
    moonshot_hitter_count = len(moonshot_slots) - moonshot_pitcher_count

    # V3.2: Log cross-lineup correlation result
    if s5_correlation_teams:
        moonshot_names = {s.candidate.player_name for s in moonshot_slots}
        for team in s5_correlation_teams:
            s5_player = next(
                (s.candidate.player_name for s in starting_5.slots
                 if s.candidate.team.upper() == team),
                "?",
            )
            moon_player = next(
                (s.candidate.player_name for s in moonshot_slots
                 if s.candidate.team.upper() == team),
                None,
            )
            if moon_player:
                logger.info(
                    "V3.2 cross-lineup correlation: %s → S5=%s, Moonshot=%s",
                    team, s5_player, moon_player,
                )

    moonshot = FilterOptimizedLineup(
        slots=moonshot_slots,
        total_expected_value=round(moonshot_total_ev, 2),
        strategy="moonshot",
        slate_classification=slate_classification,
        composition={"pitchers": moonshot_pitcher_count, "hitters": moonshot_hitter_count},
        warnings=moonshot_warnings,
    )

    return DualFilterOptimizedResult(starting_5=starting_5, moonshot=moonshot)
