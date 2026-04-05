"""
Draft lineup optimizer.

Given a set of available cards (player_name + card_boost),
produces two lineups from the same ranked pool:

  Starting 5 — Best expected value. Most likely to win any slate.
  Moonshot   — Completely different 5. Leans into TARGET players,
               sharp underground signals, HR power, and game-level
               diversification from Starting 5.

Both use the same scoring engine, same popularity model, same
rearrangement inequality for slot assignment.

For the full "Filter, Not Forecast" pipeline, use the filter_strategy
service (app/services/filter_strategy.py) which implements all 5 filters
from the Master Strategy Document.
"""

from dataclasses import dataclass, field

from app.core.constants import (
    SLOT_MULTIPLIERS,
    PITCHER_POSITIONS,
    MIN_SCORE_THRESHOLD,
    MIN_SCORE_PENALTY,
)
from app.core.utils import compute_total_value
from app.services.scoring_engine import PlayerScoreResult
from app.services.popularity import PopularityClass


# ---------------------------------------------------------------------------
# Popularity EV adjustments
# ---------------------------------------------------------------------------

# Starting 5: standard adjustments
POPULARITY_ADJUSTMENTS = {
    PopularityClass.FADE: 0.75,     # 25% EV penalty — crowd is already here
    PopularityClass.NEUTRAL: 1.0,   # no adjustment
    PopularityClass.TARGET: 1.15,   # 15% EV bonus — under the radar edge
}

# Moonshot: heavier anti-popularity lean + sharp signal boost
MOONSHOT_POPULARITY_ADJUSTMENTS = {
    PopularityClass.FADE: 0.60,     # 40% EV penalty — hard fade the crowd
    PopularityClass.NEUTRAL: 0.95,  # slight penalty — if you're not a TARGET, step aside
    PopularityClass.TARGET: 1.30,   # 30% EV bonus — under the radar is the whole point
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CardWithScore:
    player_name: str
    card_boost: float
    score_result: PlayerScoreResult
    expected_value: float = 0.0  # total_score * (2 + card_boost)
    popularity: PopularityClass = PopularityClass.NEUTRAL
    sharp_score: float = 0.0     # underground signal (0-100), used by Moonshot


@dataclass
class SlotAssignment:
    slot_index: int
    slot_mult: float
    card: CardWithScore
    expected_slot_value: float  # slot_mult * expected_value


@dataclass
class OptimizedLineup:
    slots: list[SlotAssignment]
    total_expected_value: float
    strategy: str


@dataclass
class DualLineup:
    starting_5: OptimizedLineup
    moonshot: OptimizedLineup


# ---------------------------------------------------------------------------
# EV computation
# ---------------------------------------------------------------------------

def compute_expected_value(score_result: PlayerScoreResult, card_boost: float) -> float:
    """Compute ranking signal: total_score * (2 + card_boost). Not an RS prediction."""
    ev = compute_total_value(score_result.total_score, card_boost)
    # Low-score penalty: a bad player with a great boost is often a trap
    if score_result.total_score < MIN_SCORE_THRESHOLD:
        ev *= MIN_SCORE_PENALTY
    return ev


def _get_trait_score(score_result: PlayerScoreResult, trait_name: str) -> float:
    """Extract a specific trait score from a PlayerScoreResult."""
    for t in score_result.traits:
        if t.name == trait_name:
            return t.score
    return 0.0


# ---------------------------------------------------------------------------
# Slot assignment (shared by both strategies)
# ---------------------------------------------------------------------------

def _assign_to_slots(cards: list[CardWithScore], strategy: str) -> OptimizedLineup:
    """Assign cards to slots via rearrangement inequality."""
    if not cards:
        return OptimizedLineup(slots=[], total_expected_value=0.0, strategy=strategy)

    sorted_cards = sorted(cards, key=lambda c: c.expected_value, reverse=True)
    top_cards = sorted_cards[:5]
    slot_mults = sorted(SLOT_MULTIPLIERS.items(), key=lambda x: x[1], reverse=True)

    slots = []
    total = 0.0
    for i, card in enumerate(top_cards):
        slot_idx, slot_mult = slot_mults[i]
        slot_value = slot_mult * card.expected_value
        slots.append(SlotAssignment(
            slot_index=slot_idx,
            slot_mult=slot_mult,
            card=card,
            expected_slot_value=round(slot_value, 2),
        ))
        total += slot_value

    return OptimizedLineup(
        slots=sorted(slots, key=lambda s: s.slot_index),
        total_expected_value=round(total, 2),
        strategy=strategy,
    )


# ---------------------------------------------------------------------------
# Starting 5 — best expected value
# ---------------------------------------------------------------------------

def optimize_lineup(
    cards: list[CardWithScore],
    strategy: str = "maximize_ev",
) -> OptimizedLineup:
    """
    Optimal slot assignment via the rearrangement inequality.

    Ranks on total_score * (2 + card_boost) with popularity adjustments.
    This is a filter/ranking signal, not an RS prediction.
    """
    if not cards:
        return OptimizedLineup(slots=[], total_expected_value=0.0, strategy=strategy)

    for card in cards:
        raw_ev = compute_expected_value(card.score_result, card.card_boost)
        pop_adj = POPULARITY_ADJUSTMENTS.get(card.popularity, 1.0)
        card.expected_value = raw_ev * pop_adj

    return _assign_to_slots(cards, strategy)


# ---------------------------------------------------------------------------
# Moonshot — completely different 5, anti-crowd, sharp-signal, HR power
# ---------------------------------------------------------------------------

def _compute_moonshot_ev(card: CardWithScore) -> float:
    """
    Moonshot expected value. Same base formula, but:
    1. Heavier anti-popularity lean (FADE=0.60, TARGET=1.30)
    2. Sharp signal bonus (underground buzz → +20% max)
    3. HR power tiebreaker (power_profile or k_rate trait → +10% max)
    """
    raw_ev = compute_expected_value(card.score_result, card.card_boost)

    # 1. Popularity adjustment (stronger than Starting 5)
    pop_adj = MOONSHOT_POPULARITY_ADJUSTMENTS.get(card.popularity, 0.95)

    # 2. Sharp signal bonus: 0-100 score → 0-25% EV boost
    sharp_bonus = 1.0 + (card.sharp_score / 100.0) * 0.25

    # 3. HR power / K-rate tiebreaker: reward explosive potential
    #    Batters: use power_profile trait (0-25 score → 0-10% boost)
    #    Pitchers: use k_rate trait (0-25 score → 0-10% boost)
    is_pitcher = card.score_result.position in PITCHER_POSITIONS
    if is_pitcher:
        explosive_trait = _get_trait_score(card.score_result, "k_rate")
    else:
        explosive_trait = _get_trait_score(card.score_result, "power_profile")
    # Normalize trait (max is 25) to a 0-10% bonus
    explosive_bonus = 1.0 + (explosive_trait / 25.0) * 0.10

    return raw_ev * pop_adj * sharp_bonus * explosive_bonus


def optimize_moonshot(
    cards: list[CardWithScore],
    exclude_players: set[str] | None = None,
    exclude_teams: set[str] | None = None,
) -> OptimizedLineup:
    """
    Moonshot lineup: completely different 5 from Starting 5.

    1. Excludes all Starting 5 players
    2. Prefers different games (soft penalty for same teams as Starting 5)
    3. Heavier TARGET bonus, heavier FADE penalty
    4. Sharp underground signal boosts EV
    5. Power (batters) / K-rate (pitchers) as tiebreaker
    """
    exclude_players = exclude_players or set()
    exclude_teams = exclude_teams or set()

    # Filter out Starting 5 players
    pool = [c for c in cards if c.player_name not in exclude_players]

    if not pool:
        return OptimizedLineup(slots=[], total_expected_value=0.0, strategy="moonshot")

    for card in pool:
        card.expected_value = _compute_moonshot_ev(card)

        # Game diversification: soft penalty if this player's team was in Starting 5
        # Both the player's team and opponent could overlap — penalize same-game exposure
        if card.score_result.team in exclude_teams:
            card.expected_value *= 0.85  # 15% penalty for same-game overlap

    return _assign_to_slots(pool, "moonshot")


# ---------------------------------------------------------------------------
# Dual optimizer — one call, two lineups
# ---------------------------------------------------------------------------

def optimize_dual(
    cards: list[CardWithScore],
) -> DualLineup:
    """
    Produce both Starting 5 and Moonshot from the same candidate pool.

    Starting 5: Best EV, standard popularity adjustments.
    Moonshot: Completely different 5 players, heavier anti-crowd lean,
              sharp signal boost, HR power tiebreaker, game diversification.
    """
    # Phase 1: Starting 5
    starting_5 = optimize_lineup(cards, strategy="maximize_ev")

    # Extract who Starting 5 picked (for exclusion)
    s5_players = {s.card.player_name for s in starting_5.slots}
    s5_teams = {s.card.score_result.team for s in starting_5.slots}

    # Phase 2: Moonshot from the remaining pool
    moonshot = optimize_moonshot(
        cards,
        exclude_players=s5_players,
        exclude_teams=s5_teams,
    )

    return DualLineup(starting_5=starting_5, moonshot=moonshot)


# ---------------------------------------------------------------------------
# Evaluate (user-proposed lineup)
# ---------------------------------------------------------------------------

def evaluate_lineup(
    cards_in_order: list[CardWithScore],
) -> OptimizedLineup:
    """
    Evaluate a user-proposed lineup (cards already in slot order 1-5).
    Returns expected values without reordering.
    """
    slot_mults = sorted(SLOT_MULTIPLIERS.items(), key=lambda x: x[0])

    slots = []
    total = 0.0
    for i, card in enumerate(cards_in_order[:5]):
        slot_idx, slot_mult = slot_mults[i]
        ev = compute_expected_value(card.score_result, card.card_boost)
        slot_value = slot_mult * ev
        slots.append(SlotAssignment(
            slot_index=slot_idx,
            slot_mult=slot_mult,
            card=card,
            expected_slot_value=round(slot_value, 2),
        ))
        total += slot_value

    return OptimizedLineup(
        slots=slots,
        total_expected_value=round(total, 2),
        strategy="user_proposed",
    )
