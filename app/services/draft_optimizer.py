"""
Draft lineup evaluator (user-proposed lineups only).

Used exclusively by the `/api/draft/evaluate` endpoint to score a user's
hand-built 5-card lineup and warn if slot assignment is suboptimal.

NOT the T-65 optimizer. All automated lineup construction (Starting 5 +
Moonshot, popularity gating, env/trait EV) lives in
`app/services/filter_strategy.py`.
"""

from dataclasses import dataclass

from app.core.constants import (
    SLOT_MULTIPLIERS,
    MIN_SCORE_THRESHOLD,
    MIN_SCORE_PENALTY_FLOOR,
)
from app.core.utils import BASE_MULTIPLIER, compute_total_value
from app.services.scoring_engine import PlayerScoreResult
from app.services.popularity import PopularityClass


# Popularity EV adjustments for the user-proposed-lineup warning path.
POPULARITY_ADJUSTMENTS = {
    PopularityClass.FADE: 0.75,     # 25% EV penalty — crowd is already here
    PopularityClass.NEUTRAL: 1.0,   # no adjustment
    PopularityClass.TARGET: 1.15,   # 15% EV bonus — under the radar edge
}


@dataclass
class CardWithScore:
    player_name: str
    card_boost: float
    score_result: PlayerScoreResult
    expected_value: float = 0.0  # total_score * (2 + card_boost)
    popularity: PopularityClass = PopularityClass.NEUTRAL


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


def compute_expected_value(score_result: PlayerScoreResult, card_boost: float) -> float:
    """Compute ranking signal: total_score * (2 + card_boost). Not an RS prediction."""
    ev = compute_total_value(score_result.total_score, card_boost)
    # Graduated low-score penalty: linear from FLOOR at score=0 to 1.0 at threshold
    if score_result.total_score < MIN_SCORE_THRESHOLD:
        ratio = max(0.0, score_result.total_score) / MIN_SCORE_THRESHOLD
        ev *= MIN_SCORE_PENALTY_FLOOR + ratio * (1.0 - MIN_SCORE_PENALTY_FLOOR)
    return ev


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
        # slot_value = intrinsic × (slot_mult + card_boost); reverse expected_value to recover intrinsic
        intrinsic = card.expected_value / (BASE_MULTIPLIER + card.card_boost)
        slot_value = intrinsic * (slot_mult + card.card_boost)
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
        intrinsic = ev / (BASE_MULTIPLIER + card.card_boost)
        slot_value = intrinsic * (slot_mult + card.card_boost)
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
