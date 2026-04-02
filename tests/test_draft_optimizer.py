"""Tests for the draft optimizer."""

from app.services.draft_optimizer import (
    CardWithScore,
    optimize_lineup,
    optimize_moonshot,
    optimize_dual,
    evaluate_lineup,
    compute_expected_value,
    _compute_moonshot_ev,
    POPULARITY_ADJUSTMENTS,
    MOONSHOT_POPULARITY_ADJUSTMENTS,
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
    sharp_score: float = 0.0,
    traits: list[TraitResult] | None = None,
) -> CardWithScore:
    return CardWithScore(
        player_name=name,
        card_boost=boost,
        score_result=_make_score(name, total_score=score, team=team, position=position, traits=traits),
        popularity=popularity,
        sharp_score=sharp_score,
    )


# ---------------------------------------------------------------------------
# Starting 5 tests (existing)
# ---------------------------------------------------------------------------

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
    """Colson Montgomery: score 90, +3.0x boost → EV = 90 * 5 = 450."""
    sr = _make_score("Colson Montgomery", total_score=90.0)
    ev = compute_expected_value(sr, 3.0)
    assert ev == 450.0  # 90 * (2 + 3)

    cards = [CardWithScore("Montgomery", 3.0, sr)]
    result = optimize_lineup(cards)
    assert result.slots[0].expected_slot_value == 900.0  # 450 * 2.0 slot mult


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


# ---------------------------------------------------------------------------
# Moonshot tests
# ---------------------------------------------------------------------------

def test_moonshot_excludes_starting_5():
    """Moonshot must not contain any player from Starting 5."""
    cards = [
        _make_card("Star1", score=90.0, team="NYY"),
        _make_card("Star2", score=85.0, team="LAD"),
        _make_card("Star3", score=75.0, team="BOS"),
        _make_card("Star4", score=65.0, team="HOU"),
        _make_card("Star5", score=55.0, team="CHC"),
        _make_card("Alt1", score=50.0, team="SEA"),
        _make_card("Alt2", score=45.0, team="MIA"),
        _make_card("Alt3", score=40.0, team="TB"),
        _make_card("Alt4", score=35.0, team="CIN"),
        _make_card("Alt5", score=30.0, team="COL"),
    ]

    dual = optimize_dual(cards)

    s5_names = {s.card.player_name for s in dual.starting_5.slots}
    moon_names = {s.card.player_name for s in dual.moonshot.slots}

    # Zero overlap
    assert s5_names & moon_names == set()
    assert len(dual.starting_5.slots) == 5
    assert len(dual.moonshot.slots) == 5


def test_moonshot_penalizes_same_game():
    """Moonshot should soft-penalize players from games already in Starting 5."""
    # Two players from NYY — one will be in S5, the other should be penalized in Moonshot
    cards = [
        _make_card("Judge", score=90.0, team="NYY"),   # S5
        _make_card("Star2", score=80.0, team="LAD"),    # S5
        _make_card("Star3", score=70.0, team="BOS"),    # S5
        _make_card("Star4", score=60.0, team="HOU"),    # S5
        _make_card("Star5", score=55.0, team="CHC"),    # S5
        _make_card("NYY_guy", score=50.0, team="NYY"),  # Same game as Judge
        _make_card("SEA_guy", score=50.0, team="SEA"),  # Different game
        _make_card("Alt3", score=40.0, team="MIA"),
        _make_card("Alt4", score=35.0, team="TB"),
        _make_card("Alt5", score=30.0, team="CIN"),
    ]

    dual = optimize_dual(cards)
    moon_names = [s.card.player_name for s in dual.moonshot.slots]

    # SEA_guy should rank above NYY_guy in Moonshot (same RS, but NYY penalized)
    if "SEA_guy" in moon_names and "NYY_guy" in moon_names:
        sea_slot = next(s for s in dual.moonshot.slots if s.card.player_name == "SEA_guy")
        nyy_slot = next(s for s in dual.moonshot.slots if s.card.player_name == "NYY_guy")
        assert sea_slot.slot_mult >= nyy_slot.slot_mult


def test_moonshot_target_gets_bigger_boost():
    """In Moonshot, TARGET players should get a 30% boost (vs 15% in Starting 5)."""
    # Two identical players, one TARGET one NEUTRAL
    target = _make_card("Hidden", score=60.0, popularity=PopularityClass.TARGET)
    neutral = _make_card("Vanilla", score=60.0, popularity=PopularityClass.NEUTRAL)

    moon = optimize_moonshot([target, neutral])

    slot1 = next(s for s in moon.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Hidden"


def test_moonshot_fade_gets_harder_penalty():
    """In Moonshot, FADE penalty is 40% (vs 25% in Starting 5)."""
    assert MOONSHOT_POPULARITY_ADJUSTMENTS[PopularityClass.FADE] == 0.60
    assert POPULARITY_ADJUSTMENTS[PopularityClass.FADE] == 0.75


def test_moonshot_sharp_signal_boosts_ev():
    """A player with high sharp_score should outrank an identical player without."""
    power_trait = TraitResult("power_profile", 15.0, 25.0, "HR/PA=0.040")
    sharp = _make_card("Underground", score=60.0, sharp_score=80.0, traits=[power_trait])
    plain = _make_card("Nobody", score=60.0, sharp_score=0.0, traits=[power_trait])

    # Both have same raw EV, but sharp signal gives Underground a boost
    sharp_ev = _compute_moonshot_ev(sharp)
    plain_ev = _compute_moonshot_ev(plain)

    assert sharp_ev > plain_ev


def test_moonshot_power_trait_boosts_ev():
    """Batters with high power_profile should get a tiebreaker boost in Moonshot."""
    high_power = [TraitResult("power_profile", 25.0, 25.0, "HR/PA=0.060")]
    no_power = [TraitResult("power_profile", 5.0, 25.0, "HR/PA=0.010")]

    slugger = _make_card("Slugger", score=60.0, traits=high_power)
    slapper = _make_card("Slapper", score=60.0, traits=no_power)

    slugger_ev = _compute_moonshot_ev(slugger)
    slapper_ev = _compute_moonshot_ev(slapper)

    assert slugger_ev > slapper_ev


def test_moonshot_krate_boosts_pitcher():
    """Pitchers with high k_rate should get a tiebreaker boost in Moonshot."""
    high_k = [TraitResult("k_rate", 25.0, 25.0, "K/9=12.0")]
    low_k = [TraitResult("k_rate", 5.0, 25.0, "K/9=6.5")]

    flamethrower = _make_card("Flamethrower", score=60.0, position="P", traits=high_k)
    softie = _make_card("Softie", score=60.0, position="P", traits=low_k)

    flame_ev = _compute_moonshot_ev(flamethrower)
    soft_ev = _compute_moonshot_ev(softie)

    assert flame_ev > soft_ev


def test_dual_both_lineups_competitive():
    """Both lineups should have positive expected value."""
    cards = [_make_card(f"Player{i}", score=80.0 - i * 5, team=f"T{i}") for i in range(12)]

    dual = optimize_dual(cards)

    assert dual.starting_5.total_expected_value > 0
    assert dual.moonshot.total_expected_value > 0
    assert dual.starting_5.strategy == "maximize_ev"
    assert dual.moonshot.strategy == "moonshot"


def test_dual_with_exact_10_cards():
    """With exactly 10 cards, both lineups should have exactly 5 players."""
    cards = [_make_card(f"P{i}", score=60.0, team=f"T{i}") for i in range(10)]

    dual = optimize_dual(cards)

    assert len(dual.starting_5.slots) == 5
    assert len(dual.moonshot.slots) == 5

    s5_names = {s.card.player_name for s in dual.starting_5.slots}
    moon_names = {s.card.player_name for s in dual.moonshot.slots}
    assert s5_names & moon_names == set()
