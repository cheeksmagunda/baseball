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
    SLATE_COMPOSITION,
    BOOST_NO_ENV_PENALTY,
    ENV_PASS_THRESHOLD,
    MIN_GAMES_REPRESENTED,
    SAME_GAME_EXCESS_PENALTY,
    MIN_SCORE_THRESHOLD,
    MIN_SCORE_PENALTY,
    PITCHER_ENV_WEAK_OPP_OPS,
    PITCHER_ENV_WEAK_OPP_K_PCT,
    PITCHER_ENV_MIN_K_PER_9,
    PITCHER_ENV_FRIENDLY_PARK,
    BATTER_ENV_HIGH_VEGAS_TOTAL,
    BATTER_ENV_WEAK_PITCHER_ERA,
    BATTER_ENV_TOP_LINEUP,
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
    GHOST_ENV_BONUS,
    GHOST_MOONSHOT_ENV_BONUS,
    LOW_DRAFT_THRESHOLD,
    LOW_DRAFT_BONUS,
    CHALK_DRAFT_THRESHOLD,
    CHALK_PENALTY,
    CHALK_EXEMPT_MIN_BOOST,
    BOOST_CONCENTRATION_THRESHOLD,
    BOOST_CONCENTRATION_PENALTY,
    SLOT1_DIFFERENTIATOR_EV_THRESHOLD,
)
from app.core.utils import BASE_MULTIPLIER, compute_total_value, get_trait_score
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
class SlateClassification:
    slate_type: SlateType
    game_count: int
    quality_sp_matchups: int = 0
    high_total_games: int = 0
    reason: str = ""


def classify_slate(
    game_count: int,
    games: list[dict] | None = None,
) -> SlateClassification:
    """
    Classify the slate BEFORE looking at any individual player.

    Strategy doc §4.2 Filter 1:
    - Tiny (1-3 games): limited pool, heavy team-stack
    - Pitcher Day (5+ quality SP matchups): go 4-5 pitchers
    - Hitter Day (5+ games with O/U >= 9.0): go 4-5 hitters
    - Standard (10+ games, mixed): 2-3P + 2-3 hitters

    Args:
        game_count: Number of games on the slate.
        games: List of game dicts with optional keys:
            vegas_total, home_starter_era, away_starter_era,
            home_starter_k_per_9, away_starter_k_per_9,
            home_team_ops, away_team_ops
    """
    games = games or []

    # Count quality SP matchups: ace (ERA < 3.5) facing a team with OPS < .700
    quality_sp = 0
    high_total = 0
    for g in games:
        vt = g.get("vegas_total")
        if vt is not None and vt >= HITTER_DAY_VEGAS_TOTAL_THRESHOLD:
            high_total += 1

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

    # Classification logic
    if game_count <= TINY_SLATE_MAX_GAMES:
        return SlateClassification(
            slate_type=SlateType.TINY,
            game_count=game_count,
            quality_sp_matchups=quality_sp,
            high_total_games=high_total,
            reason=f"Tiny slate ({game_count} games). Stack the favorite.",
        )

    if quality_sp >= PITCHER_DAY_MIN_QUALITY_SP:
        return SlateClassification(
            slate_type=SlateType.PITCHER_DAY,
            game_count=game_count,
            quality_sp_matchups=quality_sp,
            high_total_games=high_total,
            reason=f"Pitcher day: {quality_sp} quality SP matchups. Go 4-5 pitchers.",
        )

    if high_total >= HITTER_DAY_MIN_HIGH_TOTAL:
        return SlateClassification(
            slate_type=SlateType.HITTER_DAY,
            game_count=game_count,
            quality_sp_matchups=quality_sp,
            high_total_games=high_total,
            reason=f"Hitter day: {high_total} games with O/U >= {HITTER_DAY_VEGAS_TOTAL_THRESHOLD}. Go 4-5 hitters.",
        )

    return SlateClassification(
        slate_type=SlateType.STANDARD,
        game_count=game_count,
        quality_sp_matchups=quality_sp,
        high_total_games=high_total,
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
) -> tuple[float, list[str]]:
    """
    Compute environmental score for a batter (0-1.0).

    Strategy doc §4.2 Filter 2 batter conditions:
    - Playing in high Vegas total game (O/U >= 8.5)
    - Facing a weak opposing starter (high ERA)
    - Having a platoon advantage
    - Batting in top 4 of lineup
    - Hitter-friendly park or favorable weather (wind out, warm temp)
    """
    score = 0.0
    factors = []
    max_score = 5.0

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

    # 4. Top of lineup
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
    traits: list = field(default_factory=list)  # TraitScore list from scoring engine

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
    Compute EV through the full filter pipeline (Filters 2-4).

    Strategy doc §4.2 Filters 2-4 combined:
    1. Base EV = total_score × (2 + card_boost)
    2. Low-score penalty (same as existing)
    3. Environmental gating: boost without env support = trap (§3.5)
    4. Web-scraped popularity adjustment (FADE/TARGET/NEUTRAL)
    5. Debut/return premium (§2.3 Condition C)
    """
    base_ev = compute_total_value(candidate.total_score, candidate.card_boost)

    # Low-score penalty (existing behavior)
    if candidate.total_score < MIN_SCORE_THRESHOLD:
        base_ev *= MIN_SCORE_PENALTY

    # Filter 4: Boost-environment gating (§3.5 "Boost Trap")
    if candidate.card_boost >= 1.0 and candidate.env_score < ENV_PASS_THRESHOLD:
        base_ev *= BOOST_NO_ENV_PENALTY

    # Filter 3: Web-scraped popularity (FADE=0.75, TARGET=1.15)
    pop_adj = _popularity_ev_adjustment(candidate.popularity)
    base_ev *= pop_adj

    # Filter 3b: Draft-count ownership leverage (§2.2, §4.2 Filter 3)
    # Distinct from web-scraped popularity — this is actual contest ownership.
    if candidate.drafts is not None:
        if candidate.drafts < GHOST_DRAFT_THRESHOLD and candidate.env_score >= ENV_PASS_THRESHOLD:
            base_ev *= GHOST_ENV_BONUS
        elif candidate.drafts < LOW_DRAFT_THRESHOLD:
            base_ev *= LOW_DRAFT_BONUS
        elif candidate.drafts >= CHALK_DRAFT_THRESHOLD:
            if not (candidate.env_score >= ENV_PASS_THRESHOLD and candidate.card_boost >= CHALK_EXEMPT_MIN_BOOST):
                base_ev *= CHALK_PENALTY

    # Debut/return premium (§2.3 Condition C)
    if candidate.is_debut_or_return:
        base_ev *= DEBUT_RETURN_EV_BONUS

    return base_ev


def _enforce_composition(
    candidates: list[FilteredCandidate],
    slate_class: SlateClassification,
) -> list[FilteredCandidate]:
    """
    Enforce composition targets from the slate classification.

    Strategy doc §4.4 Day-Type Playbook:
    - Ace Day: 4-5 pitchers
    - Bash Day: 4-5 hitters
    - Standard: 2-3 pitchers + 2-3 hitters
    - Tiny: 1-2 pitchers

    Selects the top candidates by filter_ev while respecting
    min/max pitcher counts.
    """
    comp = SLATE_COMPOSITION.get(slate_class.slate_type.value, SLATE_COMPOSITION["standard"])
    min_p = comp["min_pitchers"]
    max_p = comp["max_pitchers"]

    pitchers = [c for c in candidates if c.is_pitcher]
    hitters = [c for c in candidates if not c.is_pitcher]

    # Sort each group by filter_ev descending
    pitchers.sort(key=lambda c: c.filter_ev, reverse=True)
    hitters.sort(key=lambda c: c.filter_ev, reverse=True)

    # Build lineup respecting composition
    selected_pitchers = pitchers[:max_p]
    selected_hitters = hitters[:(5 - min_p)]

    # Ensure minimum pitchers
    if len(selected_pitchers) < min_p:
        # Not enough pitchers; take what we have
        selected_pitchers = pitchers[:]

    # Fill to 5
    lineup = []
    # First, take minimum pitchers
    lineup.extend(selected_pitchers[:min_p])
    # Then fill remaining from combined pool sorted by EV
    remaining_pitchers = selected_pitchers[min_p:]
    remaining = remaining_pitchers + selected_hitters
    remaining.sort(key=lambda c: c.filter_ev, reverse=True)
    spots_left = 5 - len(lineup)
    lineup.extend(remaining[:spots_left])

    # Enforce max pitchers: if we have too many, swap worst pitcher for best hitter
    pitcher_count = sum(1 for c in lineup if c.is_pitcher)
    while pitcher_count > max_p and hitters:
        # Find the worst pitcher in lineup
        lineup_pitchers = [(i, c) for i, c in enumerate(lineup) if c.is_pitcher]
        if not lineup_pitchers:
            break
        worst_idx, worst_p = min(lineup_pitchers, key=lambda x: x[1].filter_ev)
        # Find best hitter not in lineup
        lineup_names = {c.player_name for c in lineup}
        available_hitters = [h for h in hitters if h.player_name not in lineup_names]
        if not available_hitters:
            break
        lineup[worst_idx] = available_hitters[0]
        pitcher_count -= 1

    return lineup[:5]


def _apply_game_diversification(
    lineup: list[FilteredCandidate],
) -> list[str]:
    """
    Check game diversification (Commandment 10).

    Returns warnings if all 5 players are in the same game.
    Applies soft penalty for 4th+ player from same game.
    """
    warnings = []
    if not lineup:
        return warnings

    # Count players per game
    game_counts: dict[str | int | None, int] = {}
    for c in lineup:
        gid = c.game_id
        if gid is not None:
            game_counts[gid] = game_counts.get(gid, 0) + 1

    games_represented = len(game_counts) if game_counts else 0

    if games_represented < MIN_GAMES_REPRESENTED and len(lineup) >= 5:
        warnings.append(
            f"Only {games_represented} game(s) represented. "
            f"Strategy recommends at least {MIN_GAMES_REPRESENTED}. "
            f"A pitcher's duel kills concentrated lineups."
        )

    # Apply soft penalty for 4th+ player from same game
    for gid, count in game_counts.items():
        if count >= 4:
            warnings.append(
                f"Game {gid} has {count} players. "
                f"Consider diversifying across 2-3 favorable games."
            )
            for c in lineup:
                if c.game_id == gid:
                    c.filter_ev *= SAME_GAME_EXCESS_PENALTY

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

    # Step 1: Compute filter-adjusted EV
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
    Moonshot EV through the filter pipeline. Same base as Starting 5 but:
    1. Heavier anti-popularity lean (FADE=0.60, TARGET=1.30)
    2. Sharp signal bonus (underground buzz -> up to +25% EV)
    3. Explosive bonus (power_profile or k_rate trait -> up to +10% EV)
    """
    base_ev = compute_total_value(candidate.total_score, candidate.card_boost)

    # Low-score penalty (same as Starting 5)
    if candidate.total_score < MIN_SCORE_THRESHOLD:
        base_ev *= MIN_SCORE_PENALTY

    # Boost-environment gating (same as Starting 5)
    if candidate.card_boost >= 1.0 and candidate.env_score < ENV_PASS_THRESHOLD:
        base_ev *= BOOST_NO_ENV_PENALTY

    # Moonshot popularity adjustment (heavier than Starting 5)
    pop_adj = _moonshot_popularity_adj(candidate.popularity)
    base_ev *= pop_adj

    # Draft-count ownership leverage (heavier ghost bonus for Moonshot)
    if candidate.drafts is not None:
        if candidate.drafts < GHOST_DRAFT_THRESHOLD and candidate.env_score >= ENV_PASS_THRESHOLD:
            base_ev *= GHOST_MOONSHOT_ENV_BONUS
        elif candidate.drafts < LOW_DRAFT_THRESHOLD:
            base_ev *= LOW_DRAFT_BONUS
        elif candidate.drafts >= CHALK_DRAFT_THRESHOLD:
            if not (candidate.env_score >= ENV_PASS_THRESHOLD and candidate.card_boost >= CHALK_EXEMPT_MIN_BOOST):
                base_ev *= CHALK_PENALTY

    # Debut/return premium
    if candidate.is_debut_or_return:
        base_ev *= DEBUT_RETURN_EV_BONUS

    # Sharp signal bonus: 0-100 score -> 0-25% EV boost
    sharp_bonus = 1.0 + (candidate.sharp_score / 100.0) * MOONSHOT_SHARP_BONUS_MAX

    # Explosive bonus: power_profile (batters) or k_rate (pitchers)
    if candidate.is_pitcher:
        explosive_trait = get_trait_score(candidate.traits, "k_rate")
    else:
        explosive_trait = get_trait_score(candidate.traits, "power_profile")
    # Normalize trait (max is 25) to a 0-10% bonus
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
