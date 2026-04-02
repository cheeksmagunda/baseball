"""
Draft lineup optimizer.

Given a set of available cards (player_name + card_boost),
select 5 and assign to slots (mult 2.0, 1.8, 1.6, 1.4, 1.2)
to maximize expected total lineup value.
"""

from dataclasses import dataclass, field

from app.core.constants import SLOT_MULTIPLIERS
from app.core.utils import compute_total_value
from app.services.scoring_engine import PlayerScoreResult
from app.services.popularity import PopularityClass


# Popularity penalty applied to expected value during optimization.
# FADE players get their EV reduced, TARGET players get a boost.
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
    expected_value: float = 0.0  # estimated_rs * (2 + card_boost)
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
    """Compute expected total_value for a card: estimated_rs * (2 + card_boost)."""
    return compute_total_value(score_result.estimated_rs_mid, card_boost)


def compute_floor_value(score_result: PlayerScoreResult, card_boost: float) -> float:
    """Compute floor total_value using rs_low: estimated_rs_low * (2 + card_boost)."""
    return compute_total_value(score_result.estimated_rs_low, card_boost)


def optimize_lineup(
    cards: list[CardWithScore],
    strategy: str = "maximize_ev",
) -> OptimizedLineup:
    """
    Optimal slot assignment via the rearrangement inequality:
    sort cards by expected_value descending, assign to slots by multiplier descending.

    Strategies:
    - "maximize_ev": uses estimated_rs_mid (expected case)
    - "maximize_floor": uses estimated_rs_low (worst case floor)
    """
    if not cards:
        return OptimizedLineup(slots=[], total_expected_value=0.0, strategy=strategy)

    # Compute expected values with popularity adjustment
    for card in cards:
        if strategy == "maximize_floor":
            raw_ev = compute_floor_value(card.score_result, card.card_boost)
        else:
            raw_ev = compute_expected_value(card.score_result, card.card_boost)

        # Apply popularity penalty/bonus — FADE players are docked, TARGET players boosted
        pop_adj = POPULARITY_ADJUSTMENTS.get(card.popularity, 1.0)
        card.expected_value = raw_ev * pop_adj

    # Sort by expected value, descending
    sorted_cards = sorted(cards, key=lambda c: c.expected_value, reverse=True)

    # Take top 5 (or fewer if less available)
    top_cards = sorted_cards[:5]

    # Assign to slots in order (highest EV → highest multiplier)
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
