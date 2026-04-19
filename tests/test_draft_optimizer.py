"""Tests for the draft evaluator (user-proposed lineups).

Covers `/api/draft/evaluate` helpers only. Automated lineup construction
(Starting 5 + Moonshot) is tested in `test_filter_strategy.py`.
"""

from app.services.draft_optimizer import (
    CardWithScore,
    optimize_lineup,
    evaluate_lineup,
    compute_expected_value,
    POPULARITY_ADJUSTMENTS,
)
from app.services.popularity import PopularityClass
from app.services.scoring_engine import PlayerScoreResult, TraitResult


def _make_score(
    name: str,
    total_score: float = 50.0,
    team: str = "TST",
    position: str = "OF",
    traits: list[TraitResult] | None = None,
) -> PlayerScoreResult:
    return PlayerScoreResult(
        player_name=name,
        team=team,
        position=position,
        total_score=total_score,
        traits=traits or [],
    )


def _make_card(
    name: str,
    boost: float = 0.0,
    score: float = 50.0,
    team: str = "TST",
    position: str = "OF",
    popularity: PopularityClass = PopularityClass.NEUTRAL,
    traits: list[TraitResult] | None = None,
) -> CardWithScore:
    return CardWithScore(
        player_name=name,
        card_boost=boost,
        score_result=_make_score(name, total_score=score, team=team, position=position, traits=traits),
        popularity=popularity,
    )


def test_expected_value_no_boost():
    sr = _make_score("A", total_score=50.0)
    ev = compute_expected_value(sr, 0.0)
    assert ev == 100.0  # 50 * (2 + 0)


def test_expected_value_max_boost():
    sr = _make_score("A", total_score=50.0)
    ev = compute_expected_value(sr, 3.0)
    assert ev == 250.0  # 50 * (2 + 3)


def test_optimize_assigns_best_to_highest_slot():
    cards = [
        _make_card("Ace", score=90.0),
        _make_card("Good", score=70.0),
        _make_card("Okay", score=50.0),
        _make_card("Meh", score=30.0),
        _make_card("Risky", score=10.0),
    ]

    result = optimize_lineup(cards)

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Ace"

    slot5 = next(s for s in result.slots if s.slot_index == 5)
    assert slot5.card.player_name == "Risky"


def test_boost_beats_raw_talent():
    """A +3.0x boosted mediocre player should beat an unboosted ace."""
    cards = [
        _make_card("Ace", boost=0.0, score=80.0),      # EV = 160
        _make_card("Boosted", boost=3.0, score=50.0),   # EV = 250
    ]

    result = optimize_lineup(cards)

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Boosted"


def test_optimize_fewer_than_5():
    cards = [_make_card("Only", boost=3.0, score=80.0)]
    result = optimize_lineup(cards)
    assert len(result.slots) == 1
    assert result.total_expected_value > 0


def test_optimize_empty():
    result = optimize_lineup([])
    assert len(result.slots) == 0
    assert result.total_expected_value == 0


def test_evaluate_preserves_order():
    cards = [
        _make_card("A", score=20.0),
        _make_card("B", score=40.0),
        _make_card("C", score=60.0),
        _make_card("D", score=70.0),
        _make_card("E", score=90.0),
    ]

    result = evaluate_lineup(cards)
    assert result.slots[0].card.player_name == "A"
    assert result.slots[4].card.player_name == "E"


def test_real_world_montgomery_scenario():
    """Colson Montgomery: score 90, +3.0x boost.
    ranking EV = 90 * (2+3) = 450.
    Slot 1 (mult=2.0): slot_value = 90 * (2.0 + 3.0) = 450 (additive formula)."""
    sr = _make_score("Colson Montgomery", total_score=90.0)
    ev = compute_expected_value(sr, 3.0)
    assert ev == 450.0  # 90 * (2 + 3) — ranking signal unchanged

    cards = [CardWithScore("Montgomery", 3.0, sr)]
    result = optimize_lineup(cards)
    # Additive formula: RS × (slot_mult + card_boost) = 90 × (2.0 + 3.0) = 450
    assert result.slots[0].expected_slot_value == 450.0


def test_fade_penalizes_popular_player():
    """A FADE player's EV should be reduced, causing them to drop in lineup priority."""
    popular = _make_card("Judge", score=80.0, popularity=PopularityClass.FADE)
    hidden = _make_card("Montgomery", score=80.0, popularity=PopularityClass.TARGET)

    result = optimize_lineup([popular, hidden])

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Montgomery"


def test_target_beats_neutral():
    """A TARGET player should rank above an identical NEUTRAL player."""
    target = _make_card("Caissie", boost=2.0, score=60.0, popularity=PopularityClass.TARGET)
    neutral = _make_card("Smith", boost=2.0, score=60.0, popularity=PopularityClass.NEUTRAL)

    result = optimize_lineup([target, neutral])

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Caissie"


def test_fade_with_huge_boost_can_still_win():
    """A FADE player with massive boost should still beat a TARGET with no boost."""
    fade = _make_card("Ohtani", boost=3.0, score=60.0, popularity=PopularityClass.FADE)
    target = _make_card("Rookie", boost=0.0, score=60.0, popularity=PopularityClass.TARGET)

    result = optimize_lineup([fade, target])

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Ohtani"


def test_low_score_penalty_prevents_boost_trap():
    """A truly terrible player with a huge boost should lose to a decent unboosted player.
    Graduated penalty means score=10 with 3.0x boost is no longer automatically
    a trap (that was over-penalizing ghost+boost players). But a near-zero score player
    with boost should still lose to a decent alternative.
    Trap: score=3, boost=3.0 → raw EV=15, graduated penalty ≈ 0.52 → EV ≈ 7.8
    Decent: score=16, boost=0.5 → EV=40, no penalty."""
    trap = _make_card("Trap", boost=3.0, score=3.0)       # raw EV=15, penalized→~7.8
    decent = _make_card("Decent", boost=0.5, score=16.0)  # EV=16*(2.5)=40, no penalty

    result = optimize_lineup([trap, decent])

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Decent"


def test_above_threshold_no_penalty():
    """A player just above the threshold should NOT get penalized."""
    above = _make_card("Above", boost=0.0, score=20.0)
    below = _make_card("Below", boost=0.0, score=10.0)

    result = optimize_lineup([above, below])
    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Above"


def test_fade_penalty_ratio():
    """FADE multiplier matches the documented 25% penalty."""
    assert POPULARITY_ADJUSTMENTS[PopularityClass.FADE] == 0.75
    assert POPULARITY_ADJUSTMENTS[PopularityClass.NEUTRAL] == 1.0
    assert POPULARITY_ADJUSTMENTS[PopularityClass.TARGET] == 1.15
