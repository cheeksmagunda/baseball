"""Tests for the draft optimizer."""

from app.services.draft_optimizer import (
    CardWithScore,
    optimize_lineup,
    evaluate_lineup,
    compute_expected_value,
    POPULARITY_ADJUSTMENTS,
)
from app.services.popularity import PopularityClass
from app.services.scoring_engine import PlayerScoreResult, TraitResult


def _make_score(name: str, rs_mid: float, total_score: float = 50.0) -> PlayerScoreResult:
    return PlayerScoreResult(
        player_name=name,
        team="TST",
        position="OF",
        total_score=total_score,
        estimated_rs_low=rs_mid - 1.0,
        estimated_rs_high=rs_mid + 1.0,
        estimated_rs_mid=rs_mid,
        traits=[],
    )


def test_expected_value_no_boost():
    score = _make_score("A", rs_mid=3.0)
    ev = compute_expected_value(score, 0.0)
    assert ev == 6.0  # 3.0 * (2 + 0)


def test_expected_value_max_boost():
    score = _make_score("A", rs_mid=3.0)
    ev = compute_expected_value(score, 3.0)
    assert ev == 15.0  # 3.0 * (2 + 3)


def test_optimize_assigns_best_to_highest_slot():
    cards = [
        CardWithScore("Ace", 0.0, _make_score("Ace", 5.0)),
        CardWithScore("Good", 0.0, _make_score("Good", 3.0)),
        CardWithScore("Okay", 0.0, _make_score("Okay", 2.0)),
        CardWithScore("Meh", 0.0, _make_score("Meh", 1.0)),
        CardWithScore("Risky", 0.0, _make_score("Risky", 0.5)),
    ]

    result = optimize_lineup(cards)

    # Slot 1 (mult 2.0) should get the best player
    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Ace"

    # Slot 5 (mult 1.2) should get the worst
    slot5 = next(s for s in result.slots if s.slot_index == 5)
    assert slot5.card.player_name == "Risky"


def test_boost_beats_raw_talent():
    """A +3.0x boosted mediocre player should beat an unboosted ace."""
    cards = [
        CardWithScore("Ace", 0.0, _make_score("Ace", 5.0)),      # EV = 10.0
        CardWithScore("Boosted", 3.0, _make_score("Boosted", 3.0)),  # EV = 15.0
    ]

    result = optimize_lineup(cards)

    # Boosted player should be in slot 1
    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Boosted"


def test_optimize_fewer_than_5():
    cards = [
        CardWithScore("Only", 3.0, _make_score("Only", 4.0)),
    ]
    result = optimize_lineup(cards)
    assert len(result.slots) == 1
    assert result.total_expected_value > 0


def test_optimize_empty():
    result = optimize_lineup([])
    assert len(result.slots) == 0
    assert result.total_expected_value == 0


def test_evaluate_preserves_order():
    cards = [
        CardWithScore("A", 0.0, _make_score("A", 1.0)),
        CardWithScore("B", 0.0, _make_score("B", 2.0)),
        CardWithScore("C", 0.0, _make_score("C", 3.0)),
        CardWithScore("D", 0.0, _make_score("D", 4.0)),
        CardWithScore("E", 0.0, _make_score("E", 5.0)),
    ]

    result = evaluate_lineup(cards)
    # Should preserve input order
    assert result.slots[0].card.player_name == "A"
    assert result.slots[4].card.player_name == "E"


def test_real_world_montgomery_scenario():
    """Colson Montgomery: RS 6.3, +3.0x boost, slot 1 = 31.5 total_value."""
    score = _make_score("Colson Montgomery", rs_mid=6.3)
    ev = compute_expected_value(score, 3.0)
    # EV = 6.3 * (2 + 3) = 31.5
    assert abs(ev - 31.5) < 0.01

    # In slot 1 (mult 2.0): 2.0 * 31.5 = 63.0
    cards = [CardWithScore("Montgomery", 3.0, score)]
    result = optimize_lineup(cards)
    assert result.slots[0].expected_slot_value == 63.0


def test_fade_penalizes_popular_player():
    """A FADE player's EV should be reduced, causing them to drop in lineup priority."""
    # Two players with identical raw stats
    popular = CardWithScore("Judge", 0.0, _make_score("Judge", 5.0), popularity=PopularityClass.FADE)
    hidden = CardWithScore("Montgomery", 0.0, _make_score("Montgomery", 5.0), popularity=PopularityClass.TARGET)

    result = optimize_lineup([popular, hidden])

    # TARGET player (hidden gem) should get slot 1 due to EV bonus
    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Montgomery"


def test_target_beats_neutral():
    """A TARGET player should rank above an identical NEUTRAL player."""
    target = CardWithScore("Caissie", 2.0, _make_score("Caissie", 3.0), popularity=PopularityClass.TARGET)
    neutral = CardWithScore("Smith", 2.0, _make_score("Smith", 3.0), popularity=PopularityClass.NEUTRAL)

    result = optimize_lineup([target, neutral])

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Caissie"


def test_fade_with_huge_boost_can_still_win():
    """A FADE player with massive boost should still beat a TARGET with no boost."""
    # FADE + 3.0x boost: raw EV = 3.0 * 5.0 = 15.0, after 0.75 penalty = 11.25
    fade = CardWithScore("Ohtani", 3.0, _make_score("Ohtani", 3.0), popularity=PopularityClass.FADE)
    # TARGET + 0x boost: raw EV = 3.0 * 2.0 = 6.0, after 1.15 bonus = 6.9
    target = CardWithScore("Rookie", 0.0, _make_score("Rookie", 3.0), popularity=PopularityClass.TARGET)

    result = optimize_lineup([fade, target])

    # FADE player still wins because the boost math is too strong
    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Ohtani"
