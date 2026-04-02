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
    rs_mid: float,
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
        estimated_rs_low=rs_mid - 1.0,
        estimated_rs_high=rs_mid + 1.0,
        estimated_rs_mid=rs_mid,
        traits=traits or [],
    )


def _make_card(
    name: str,
    boost: float = 0.0,
    rs_mid: float = 3.0,
    total_score: float = 50.0,
    team: str = "TST",
    position: str = "OF",
    popularity: PopularityClass = PopularityClass.NEUTRAL,
    sharp_score: float = 0.0,
    traits: list[TraitResult] | None = None,
) -> CardWithScore:
    return CardWithScore(
        player_name=name,
        card_boost=boost,
        score_result=_make_score(name, rs_mid, total_score, team, position, traits),
        popularity=popularity,
        sharp_score=sharp_score,
    )


# ---------------------------------------------------------------------------
# Starting 5 tests (existing)
# ---------------------------------------------------------------------------

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
        _make_card("Ace", rs_mid=5.0),
        _make_card("Good", rs_mid=3.0),
        _make_card("Okay", rs_mid=2.0),
        _make_card("Meh", rs_mid=1.0),
        _make_card("Risky", rs_mid=0.5),
    ]

    result = optimize_lineup(cards)

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Ace"

    slot5 = next(s for s in result.slots if s.slot_index == 5)
    assert slot5.card.player_name == "Risky"


def test_boost_beats_raw_talent():
    """A +3.0x boosted mediocre player should beat an unboosted ace."""
    cards = [
        _make_card("Ace", boost=0.0, rs_mid=5.0),      # EV = 10.0
        _make_card("Boosted", boost=3.0, rs_mid=3.0),   # EV = 15.0
    ]

    result = optimize_lineup(cards)

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Boosted"


def test_optimize_fewer_than_5():
    cards = [_make_card("Only", boost=3.0, rs_mid=4.0)]
    result = optimize_lineup(cards)
    assert len(result.slots) == 1
    assert result.total_expected_value > 0


def test_optimize_empty():
    result = optimize_lineup([])
    assert len(result.slots) == 0
    assert result.total_expected_value == 0


def test_evaluate_preserves_order():
    cards = [
        _make_card("A", rs_mid=1.0),
        _make_card("B", rs_mid=2.0),
        _make_card("C", rs_mid=3.0),
        _make_card("D", rs_mid=4.0),
        _make_card("E", rs_mid=5.0),
    ]

    result = evaluate_lineup(cards)
    assert result.slots[0].card.player_name == "A"
    assert result.slots[4].card.player_name == "E"


def test_real_world_montgomery_scenario():
    """Colson Montgomery: RS 6.3, +3.0x boost, slot 1 = 31.5 total_value."""
    score = _make_score("Colson Montgomery", rs_mid=6.3)
    ev = compute_expected_value(score, 3.0)
    assert abs(ev - 31.5) < 0.01

    cards = [CardWithScore("Montgomery", 3.0, score)]
    result = optimize_lineup(cards)
    assert result.slots[0].expected_slot_value == 63.0


def test_fade_penalizes_popular_player():
    """A FADE player's EV should be reduced, causing them to drop in lineup priority."""
    popular = _make_card("Judge", rs_mid=5.0, popularity=PopularityClass.FADE)
    hidden = _make_card("Montgomery", rs_mid=5.0, popularity=PopularityClass.TARGET)

    result = optimize_lineup([popular, hidden])

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Montgomery"


def test_target_beats_neutral():
    """A TARGET player should rank above an identical NEUTRAL player."""
    target = _make_card("Caissie", boost=2.0, rs_mid=3.0, popularity=PopularityClass.TARGET)
    neutral = _make_card("Smith", boost=2.0, rs_mid=3.0, popularity=PopularityClass.NEUTRAL)

    result = optimize_lineup([target, neutral])

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Caissie"


def test_fade_with_huge_boost_can_still_win():
    """A FADE player with massive boost should still beat a TARGET with no boost."""
    fade = _make_card("Ohtani", boost=3.0, rs_mid=3.0, popularity=PopularityClass.FADE)
    target = _make_card("Rookie", boost=0.0, rs_mid=3.0, popularity=PopularityClass.TARGET)

    result = optimize_lineup([fade, target])

    slot1 = next(s for s in result.slots if s.slot_index == 1)
    assert slot1.card.player_name == "Ohtani"


# ---------------------------------------------------------------------------
# Moonshot tests
# ---------------------------------------------------------------------------

def test_moonshot_excludes_starting_5():
    """Moonshot must not contain any player from Starting 5."""
    cards = [
        _make_card("Star1", rs_mid=5.0, team="NYY"),
        _make_card("Star2", rs_mid=4.5, team="LAD"),
        _make_card("Star3", rs_mid=4.0, team="BOS"),
        _make_card("Star4", rs_mid=3.5, team="HOU"),
        _make_card("Star5", rs_mid=3.0, team="CHC"),
        _make_card("Alt1", rs_mid=2.8, team="SEA"),
        _make_card("Alt2", rs_mid=2.5, team="MIA"),
        _make_card("Alt3", rs_mid=2.3, team="TB"),
        _make_card("Alt4", rs_mid=2.0, team="CIN"),
        _make_card("Alt5", rs_mid=1.8, team="COL"),
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
        _make_card("Judge", rs_mid=5.0, team="NYY"),   # S5
        _make_card("Star2", rs_mid=4.0, team="LAD"),    # S5
        _make_card("Star3", rs_mid=3.5, team="BOS"),    # S5
        _make_card("Star4", rs_mid=3.0, team="HOU"),    # S5
        _make_card("Star5", rs_mid=2.8, team="CHC"),    # S5
        _make_card("NYY_guy", rs_mid=2.5, team="NYY"),  # Same game as Judge
        _make_card("SEA_guy", rs_mid=2.5, team="SEA"),  # Different game
        _make_card("Alt3", rs_mid=2.0, team="MIA"),
        _make_card("Alt4", rs_mid=1.8, team="TB"),
        _make_card("Alt5", rs_mid=1.5, team="CIN"),
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
    target = _make_card("Hidden", rs_mid=3.0, popularity=PopularityClass.TARGET)
    neutral = _make_card("Vanilla", rs_mid=3.0, popularity=PopularityClass.NEUTRAL)

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
    sharp = _make_card("Underground", rs_mid=3.0, sharp_score=80.0, traits=[power_trait])
    plain = _make_card("Nobody", rs_mid=3.0, sharp_score=0.0, traits=[power_trait])

    # Both have same raw EV, but sharp signal gives Underground a boost
    sharp_ev = _compute_moonshot_ev(sharp)
    plain_ev = _compute_moonshot_ev(plain)

    assert sharp_ev > plain_ev


def test_moonshot_power_trait_boosts_ev():
    """Batters with high power_profile should get a tiebreaker boost in Moonshot."""
    high_power = [TraitResult("power_profile", 25.0, 25.0, "HR/PA=0.060")]
    no_power = [TraitResult("power_profile", 5.0, 25.0, "HR/PA=0.010")]

    slugger = _make_card("Slugger", rs_mid=3.0, traits=high_power)
    slapper = _make_card("Slapper", rs_mid=3.0, traits=no_power)

    slugger_ev = _compute_moonshot_ev(slugger)
    slapper_ev = _compute_moonshot_ev(slapper)

    assert slugger_ev > slapper_ev


def test_moonshot_krate_boosts_pitcher():
    """Pitchers with high k_rate should get a tiebreaker boost in Moonshot."""
    high_k = [TraitResult("k_rate", 25.0, 25.0, "K/9=12.0")]
    low_k = [TraitResult("k_rate", 5.0, 25.0, "K/9=6.5")]

    flamethrower = _make_card("Flamethrower", rs_mid=3.0, position="P", traits=high_k)
    softie = _make_card("Softie", rs_mid=3.0, position="P", traits=low_k)

    flame_ev = _compute_moonshot_ev(flamethrower)
    soft_ev = _compute_moonshot_ev(softie)

    assert flame_ev > soft_ev


def test_dual_both_lineups_competitive():
    """Both lineups should have positive expected value."""
    cards = [_make_card(f"Player{i}", rs_mid=3.0 - i * 0.2, team=f"T{i}") for i in range(12)]

    dual = optimize_dual(cards)

    assert dual.starting_5.total_expected_value > 0
    assert dual.moonshot.total_expected_value > 0
    assert dual.starting_5.strategy == "maximize_ev"
    assert dual.moonshot.strategy == "moonshot"


def test_dual_with_exact_10_cards():
    """With exactly 10 cards, both lineups should have exactly 5 players."""
    cards = [_make_card(f"P{i}", rs_mid=3.0, team=f"T{i}") for i in range(10)]

    dual = optimize_dual(cards)

    assert len(dual.starting_5.slots) == 5
    assert len(dual.moonshot.slots) == 5

    s5_names = {s.card.player_name for s in dual.starting_5.slots}
    moon_names = {s.card.player_name for s in dual.moonshot.slots}
    assert s5_names & moon_names == set()
