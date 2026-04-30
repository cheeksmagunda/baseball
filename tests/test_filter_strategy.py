"""
Tests for the active filter_strategy pipeline.

Covers: env score computation, slate classification, base EV computation,
composition enforcement, slot assignment, and the single-lineup optimizer.

V11.0: popularity (FADE/TARGET/NEUTRAL) and Moonshot have been removed
from the pipeline entirely; tests covering those are deleted.
"""

import pytest

from app.services.filter_strategy import (
    SlateType,
    SlateClassification,
    FilteredCandidate,
    FilterOptimizedLineup,
    classify_slate,
    compute_pitcher_env_score,
    compute_batter_env_score,
    _compute_dnp_adjustment,
    _compute_base_ev,
    _enforce_composition,
    _smart_slot_assignment,
    run_filter_strategy,
)
from app.core.constants import (
    MAX_PLAYERS_PER_TEAM_BATTERS_DEFAULT,
    PITCHER_ANCHOR_SLOT,
    SLOT_MULTIPLIERS,
    STACK_BONUS,
    DNP_RISK_PENALTY,
    DNP_UNKNOWN_PENALTY,
    ENV_UNKNOWN_COUNT_THRESHOLD,
)
from app.services.filter_strategy import StackableGame


# ---------------------------------------------------------------------------
# Helpers — build test candidates quickly
# ---------------------------------------------------------------------------

def _make_candidate(
    name: str = "Test Player",
    team: str = "NYY",
    position: str = "OF",
    is_pitcher: bool = False,
    total_score: float = 50.0,
    env_score: float = 0.6,
    game_id: int | str | None = 1,
    batting_order: int | None = 3,
    env_unknown_count: int = 0,
    is_in_blowout_game: bool = False,
    traits: list | None = None,
) -> FilteredCandidate:
    return FilteredCandidate(
        player_name=name,
        team=team,
        position="SP" if is_pitcher else position,
        total_score=total_score,
        env_score=env_score,
        env_unknown_count=env_unknown_count,
        game_id=game_id,
        is_pitcher=is_pitcher,
        traits=traits or [],
        batting_order=batting_order if not is_pitcher else None,
        is_in_blowout_game=is_in_blowout_game,
    )


def _default_slate() -> SlateClassification:
    return SlateClassification(
        slate_type=SlateType.STANDARD,
        game_count=10,
        reason="Standard test slate",
    )


def _stack_eligible_slate(favored_team: str, moneyline: int = -220, vegas_total: float = 9.5) -> SlateClassification:
    """Slate classification where `favored_team` clears the stack-eligibility gate.

    A team is stack-eligible iff its game has moneyline ≤ -200 AND O/U ≥ 9.0.
    Tests that want stacking to fire must use this helper; without it the
    default 1-batter-per-team cap applies.
    """
    return SlateClassification(
        slate_type=SlateType.HITTER_DAY,
        game_count=10,
        blowout_games=1,
        high_total_games=1,
        stackable_games=[
            StackableGame(
                game_id=None,
                favored_team=favored_team,
                moneyline=moneyline,
                vegas_total=vegas_total,
            ),
        ],
        reason="stack-eligible test slate",
    )


def _make_pool(n_pitchers: int = 2, n_batters: int = 10) -> list[FilteredCandidate]:
    """Build a realistic candidate pool with distinct teams/games.

    Pitcher env_score=0.85 (favored-team confirmed starter, typical T-65 case).
    Anything below ~0.80 is borderline; the V10.6 EV-driven chooser will flip
    to 0P+5B if the batter pool is genuinely stronger, which is correct
    behaviour but inconvenient for structural-invariant tests that want to
    verify the 1P+4B path.  These tests should drive a clear pitcher win;
    explicit-EV tests that care about marginal cases construct pools inline.
    """
    teams = ["NYY", "BOS", "LAD", "HOU", "ATL", "CHC", "SF", "SEA", "MIN", "TB", "SD", "CLE"]
    pool = []
    idx = 0
    for i in range(n_pitchers):
        pool.append(_make_candidate(
            name=f"Pitcher_{i}",
            team=teams[idx % len(teams)],
            is_pitcher=True,
            game_id=100 + idx,
            total_score=60 + i * 5,
            env_score=0.85,
        ))
        idx += 1
    for i in range(n_batters):
        pool.append(_make_candidate(
            name=f"Batter_{i}",
            team=teams[idx % len(teams)],
            is_pitcher=False,
            game_id=100 + idx,
            total_score=40 + i * 3,
            env_score=0.5 + i * 0.03,
        ))
        idx += 1
    return pool


# ===================================================================
# 1. Slate Classification
# ===================================================================

class TestSlateClassification:
    def test_tiny_slate(self):
        result = classify_slate(2)
        assert result.slate_type == SlateType.TINY
        assert result.game_count == 2

    def test_standard_slate(self):
        result = classify_slate(10)
        assert result.slate_type == SlateType.STANDARD
        assert result.game_count == 10

    def test_hitter_day_high_totals(self):
        games = [{"vegas_total": 10.0} for _ in range(5)]
        result = classify_slate(10, games=games)
        assert result.slate_type == SlateType.HITTER_DAY
        assert result.high_total_games == 5

    def test_hitter_day_blowout(self):
        # V10.5: PATH 1 stackable now requires both ML ≤ -200 AND O/U ≥ 9.0.
        # A heavy ML favorite in a low-total pitcher's duel is no longer
        # treated as a "blowout" because runs (and HR correlation) can't materialize.
        games = [{"home_moneyline": -220, "home_team": "NYY", "vegas_total": 9.5}]
        result = classify_slate(5, games=games)
        assert result.slate_type == SlateType.HITTER_DAY
        assert result.blowout_games == 1
        assert result.stackable_games[0].favored_team == "NYY"

    def test_blowout_with_low_ou_not_stackable(self):
        """Heavy ML favorite + low O/U (e.g. LAD vs MIA, ML -290 / O/U 7.5)
        is NOT a stackable blowout — fails PATH 1 OU gate."""
        games = [{"home_moneyline": -290, "home_team": "LAD", "vegas_total": 7.5}]
        result = classify_slate(5, games=games)
        assert result.blowout_games == 0
        assert result.stackable_games == []

    def test_pitcher_day(self):
        games = [
            {
                "home_starter_era": 2.5,
                "home_starter_k_per_9": 9.0,
                "away_team_ops": 0.680,
            }
            for _ in range(5)
        ]
        result = classify_slate(10, games=games)
        assert result.slate_type == SlateType.PITCHER_DAY
        assert result.quality_sp_matchups >= 4

    def test_empty_games_list(self):
        result = classify_slate(10, games=[])
        assert result.slate_type == SlateType.STANDARD


# ===================================================================
# 2. Pitcher Env Score
# ===================================================================

class TestPitcherEnvScore:
    """V12 pitcher env tests — calibrated against 222-pitcher historical audit.

    Key behaviors:
      * ML peak at -120 to -180 (mild fav) — beats heavy fav and underdog
      * Vegas O/U inverse — low total = pitcher game
      * Park HR factor — pitcher-friendly = bonus
      * K/9 talent — modest only
      * ERA tail — small lever
    """

    def test_perfect_env(self):
        # Every strong V12 signal lit: mild fav, low O/U, pitcher park, elite K/9, elite ERA.
        score, factors = compute_pitcher_env_score(
            team_moneyline=-150,           # mild fav peak → +1.0
            vegas_total=7.0,               # low total → +1.0
            park_team="LAD",               # pitcher-ish park → ≤0.95 → +0.6
            pitcher_k_per_9=11.0,          # elite → +0.4
            own_starter_era=2.5,           # elite → +0.3
            opp_team_ops=0.660,            # weak opp → +0.3
        )
        # Expected total ≈ 3.6 / 4.0 ≈ 0.90 (saturates if more signals stack)
        assert score >= 0.85
        assert any("Mild favorite" in f for f in factors)

    def test_empty_env(self):
        score, factors = compute_pitcher_env_score()
        assert score == pytest.approx(0.0, abs=0.01)
        assert factors == []

    def test_ml_peak_at_mild_fav(self):
        """V12: data shows mild fav (-180 to -120) wins HV 37.5%, heavy fav 14.5%."""
        score_mild, _ = compute_pitcher_env_score(team_moneyline=-150)
        score_heavy, _ = compute_pitcher_env_score(team_moneyline=-250)
        score_underdog, _ = compute_pitcher_env_score(team_moneyline=+150)
        assert score_mild > score_heavy, "mild fav must score higher than heavy fav (V12 audit)"
        assert score_mild > score_underdog
        # Heavy fav explicitly penalised vs underdog tossup (heavy fav 0.3 vs underdog 0.2)
        assert score_heavy > score_underdog or score_heavy >= score_underdog

    def test_vegas_total_inverse(self):
        """V12: low O/U is a pitcher game; high O/U penalises pitcher EV."""
        score_low, _ = compute_pitcher_env_score(team_moneyline=-150, vegas_total=7.0)
        score_high, _ = compute_pitcher_env_score(team_moneyline=-150, vegas_total=10.0)
        assert score_low > score_high

    def test_pitcher_park_bonus(self):
        score_pitcher_park, _ = compute_pitcher_env_score(
            team_moneyline=-150, park_team="SF"  # PF≈0.92
        )
        score_hitter_park, _ = compute_pitcher_env_score(
            team_moneyline=-150, park_team="COL"  # PF≈1.38
        )
        assert score_pitcher_park > score_hitter_park

    def test_dead_signals_no_longer_score(self):
        """V12 audit removed opp_team_k_pct (Q1 25% / Q4 23% HV — dead).
        Passing it should have no effect on score."""
        score_with_kpct, _ = compute_pitcher_env_score(
            team_moneyline=-150, opp_team_k_pct=0.26  # was a "high-K opponent" bonus
        )
        score_without_kpct, _ = compute_pitcher_env_score(team_moneyline=-150)
        assert score_with_kpct == pytest.approx(score_without_kpct, abs=0.001)


# ===================================================================
# 3. Batter Env Score
# ===================================================================

class TestBatterEnvScore:
    """V12 batter env tests — calibrated against 994-batter historical audit.

    Strong signals (audit-validated):
      * Opp starter ERA: Q1 34% HV → Q4 57% HV (+23pp swing)
      * Opp starter WHIP: Q1 39% → Q4 55% (+16pp)
      * Wind speed (real, survives park control): +12-15pp at ≥10 mph
      * Underdog premium: +ML teams produce MORE HV (inverted from intuition)

    Removed dead signals: vegas_total, opp_bullpen_era, opp_starter_k_per_9,
    series wins, L10 wins, opp rest days, compound park×temp.
    """

    def test_perfect_env(self):
        # Lit up: weak opp ERA + WHIP, hitter park, wind out, underdog, top order
        score, factors, unknown = compute_batter_env_score(
            opp_pitcher_era=6.0,             # +1.4
            opp_starter_whip=1.55,           # +0.9
            park_team="COL",                 # +0.3
            wind_speed_mph=12,
            wind_direction="OUT TO CF",      # +0.6
            team_moneyline=+150,             # +0.3 (underdog premium)
            batting_order=2,                 # +0.4
            temperature_f=80,                # +0.1
            platoon_advantage=True,          # +0.3
        )
        # Total ~4.3 / 4.0 → saturates at 1.0
        assert score >= 0.95
        assert unknown == 0
        assert any("Bloated" in f or "Weak" in f for f in factors)

    def test_empty_env_tracks_unknowns(self):
        score, _, unknown = compute_batter_env_score()
        assert score == pytest.approx(0.0, abs=0.01)
        # V12: tracks opp_pitcher_era, opp_starter_whip, batting_order = 3 critical missing
        assert unknown == 3

    def test_dead_signals_no_longer_score(self):
        """V12 deletes vegas_total, opp_bullpen_era, opp_starter_k_per_9,
        series_*, team_l10_wins, opp_team_rest_days from the env score.
        Passing them should have ZERO effect on the result."""
        score_with_dead, _, _ = compute_batter_env_score(
            opp_pitcher_era=5.0,
            opp_starter_whip=1.4,
            batting_order=3,
            # Dead signals (formerly contributed):
            vegas_total=10.0,
            opp_bullpen_era=5.5,
            opp_starter_k_per_9=6.0,
            series_team_wins=3, series_opp_wins=0,
            team_l10_wins=8,
            opp_team_rest_days=0,
        )
        score_without_dead, _, _ = compute_batter_env_score(
            opp_pitcher_era=5.0,
            opp_starter_whip=1.4,
            batting_order=3,
        )
        assert score_with_dead == pytest.approx(score_without_dead, abs=0.001)

    def test_underdog_premium_v12(self):
        """V12: data shows underdogs (ML +100+) HV 57% vs heavy favs 36%."""
        score_underdog, _, _ = compute_batter_env_score(
            opp_pitcher_era=4.0, team_moneyline=+150
        )
        score_heavy_fav, _, _ = compute_batter_env_score(
            opp_pitcher_era=4.0, team_moneyline=-250
        )
        # Underdog premium beats heavy-favorite penalty (-0.2)
        assert score_underdog > score_heavy_fav

    def test_batting_order_top_premium(self):
        """V12: batting order 1-3 gets premium, declining through tail."""
        s_top, _, _ = compute_batter_env_score(opp_pitcher_era=4.0, batting_order=2)
        s_mid, _, _ = compute_batter_env_score(opp_pitcher_era=4.0, batting_order=5)
        s_bot, _, _ = compute_batter_env_score(opp_pitcher_era=4.0, batting_order=8)
        assert s_top > s_mid > s_bot

    def test_opp_starter_whip_independent_of_era(self):
        """V12: WHIP and ERA are independent positive contributions (no soft cap)."""
        score_just_era, _, _ = compute_batter_env_score(opp_pitcher_era=5.5)
        score_era_plus_whip, _, _ = compute_batter_env_score(
            opp_pitcher_era=5.5, opp_starter_whip=1.5
        )
        assert score_era_plus_whip > score_just_era

    def test_wind_out_beats_wind_in(self):
        """V12: wind OUT bonus, wind IN penalty (both at 10+ mph)."""
        score_out, _, _ = compute_batter_env_score(
            opp_pitcher_era=4.0, wind_speed_mph=15, wind_direction="OUT TO CF"
        )
        score_in, _, _ = compute_batter_env_score(
            opp_pitcher_era=4.0, wind_speed_mph=15, wind_direction="IN FROM CF"
        )
        assert score_out > score_in

    def test_calm_wind_no_effect(self):
        """V12: wind below 6 mph contributes nothing regardless of direction."""
        score_calm_in, _, _ = compute_batter_env_score(
            opp_pitcher_era=4.0, wind_speed_mph=3, wind_direction="IN"
        )
        score_no_wind, _, _ = compute_batter_env_score(opp_pitcher_era=4.0)
        assert score_calm_in == pytest.approx(score_no_wind, abs=0.001)


# ===================================================================
# 4. DNP Adjustment
# ===================================================================

class TestDNPAdjustment:
    def test_pitcher_always_1(self):
        c = _make_candidate(is_pitcher=True, batting_order=None)
        assert _compute_dnp_adjustment(c) == 1.0

    def test_batter_with_batting_order(self):
        c = _make_candidate(batting_order=3)
        assert _compute_dnp_adjustment(c) == 1.0

    def test_batter_no_order_many_unknowns(self):
        c = _make_candidate(batting_order=None, env_unknown_count=ENV_UNKNOWN_COUNT_THRESHOLD)
        assert _compute_dnp_adjustment(c) == DNP_UNKNOWN_PENALTY

    def test_batter_no_order_few_unknowns_is_confirmed_bad(self):
        c = _make_candidate(batting_order=None, env_unknown_count=0)
        assert _compute_dnp_adjustment(c) == DNP_RISK_PENALTY


# ===================================================================
# 7. Base EV Computation
# ===================================================================

class TestBaseEV:
    def test_ev_is_positive(self):
        c = _make_candidate()
        ev = _compute_base_ev(c)
        assert ev > 0

    def test_blowout_stack_bonus_applied(self):
        normal = _make_candidate(is_in_blowout_game=False)
        blowout = _make_candidate(is_in_blowout_game=True)
        ev_normal = _compute_base_ev(normal)
        ev_blowout = _compute_base_ev(blowout)
        assert ev_blowout == pytest.approx(ev_normal * STACK_BONUS, rel=0.01)

    def test_high_env_raises_ev(self):
        low_env = _make_candidate(env_score=0.1)
        high_env = _make_candidate(env_score=0.9)
        ev_low = _compute_base_ev(low_env)
        ev_high = _compute_base_ev(high_env)
        assert ev_high > ev_low

# ===================================================================
# 6. Filter EV
# ===================================================================

class TestFilterEV:
    def test_filter_ev_positive(self):
        c = _make_candidate()
        assert _compute_base_ev(c) > 0

    def test_filter_ev_matches_base_ev(self):
        """V11.0: filter EV is the base EV.  No popularity bonus or penalty."""
        c = _make_candidate(env_score=0.7, total_score=65)
        assert _compute_base_ev(c) == pytest.approx(_compute_base_ev(c), rel=1e-9)


# ===================================================================
# 9. Composition Enforcement
# ===================================================================

class TestComposition:
    def test_exactly_5_players(self):
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        assert len(lineup) == 5

    def test_pitcher_count_in_legal_range(self):
        """V12: pitcher count is unconstrained — any of 0..5 is legal.
        The EV-driven chooser picks the best variant by slot-weighted EV."""
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        pitcher_count = sum(1 for c in lineup if c.is_pitcher)
        assert 0 <= pitcher_count <= 5

    def test_default_slate_caps_batters_at_one_per_team(self):
        """V10.1: with no stack-eligible games, every team is capped at 1 batter."""
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        from collections import Counter
        batter_team_counts = Counter(c.team for c in lineup if not c.is_pitcher)
        for team, count in batter_team_counts.items():
            assert count <= MAX_PLAYERS_PER_TEAM_BATTERS_DEFAULT, (
                f"{team} has {count} batters on a non-stack-eligible slate"
            )

    def test_stack_eligible_team_allows_mini_stack(self):
        """V10.1: a team in a blowout + high-total game may contribute up to 2 batters (mini-stack)."""
        pool = [
            _make_candidate(name="SP_0", team="BOS", is_pitcher=True, game_id=1, total_score=80, env_score=0.7),
            _make_candidate(name="NYY_1", team="NYY", is_pitcher=False, game_id=2, total_score=75, env_score=0.95),
            _make_candidate(name="NYY_2", team="NYY", is_pitcher=False, game_id=2, total_score=72, env_score=0.93),
            _make_candidate(name="NYY_3", team="NYY", is_pitcher=False, game_id=2, total_score=70, env_score=0.92),
            _make_candidate(name="NYY_4", team="NYY", is_pitcher=False, game_id=2, total_score=68, env_score=0.90),
            _make_candidate(name="LAD_1", team="LAD", is_pitcher=False, game_id=3, total_score=55, env_score=0.6),
            _make_candidate(name="HOU_1", team="HOU", is_pitcher=False, game_id=4, total_score=50, env_score=0.55),
        ]
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        slate = _stack_eligible_slate(favored_team="NYY")
        lineup = _enforce_composition(pool, slate)
        nyy = [c for c in lineup if c.team == "NYY"]
        assert len(nyy) == 2, f"NYY mini-stack should fill 2 on eligible day, got {len(nyy)}"

    def test_per_game_cap_two(self):
        """V10.1: never more than 2 batters from the same game, even across teams."""
        pool = [
            _make_candidate(name="SP_0", team="CHC", is_pitcher=True, game_id=99, total_score=80, env_score=0.7),
            # Two NYY + two BOS batters all from the same game (game_id=2)
            _make_candidate(name="NYY_1", team="NYY", is_pitcher=False, game_id=2, total_score=75, env_score=0.95),
            _make_candidate(name="NYY_2", team="NYY", is_pitcher=False, game_id=2, total_score=74, env_score=0.94),
            _make_candidate(name="BOS_1", team="BOS", is_pitcher=False, game_id=2, total_score=73, env_score=0.93),
            _make_candidate(name="BOS_2", team="BOS", is_pitcher=False, game_id=2, total_score=72, env_score=0.92),
            # Fallback picks from other games
            _make_candidate(name="LAD_1", team="LAD", is_pitcher=False, game_id=3, total_score=55, env_score=0.6),
            _make_candidate(name="HOU_1", team="HOU", is_pitcher=False, game_id=4, total_score=50, env_score=0.55),
        ]
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        # Both NYY and BOS are "stack-eligible" here so per-team is 2 for each.
        # The per-game cap must prevent all four game_id=2 picks from being drafted.
        slate = SlateClassification(
            slate_type=SlateType.HITTER_DAY,
            game_count=10,
            blowout_games=2,
            high_total_games=2,
            stackable_games=[
                StackableGame(game_id=2, favored_team="NYY", moneyline=-210, vegas_total=10.5),
                StackableGame(game_id=2, favored_team="BOS", moneyline=-210, vegas_total=10.5),
            ],
            reason="two-team stack-eligible",
        )
        lineup = _enforce_composition(pool, slate)
        from collections import Counter
        game_counts = Counter(c.game_id for c in lineup if not c.is_pitcher)
        assert all(v <= 2 for v in game_counts.values()), f"Per-game cap violated: {game_counts}"

    def test_stack_eligible_requires_both_moneyline_and_total(self):
        """V10.2: a blowout favorite alone (PATH 1) requires O/U ≥ 9.0;
        a low total below the shootout threshold (10.5) also fails PATH 2.
        With ML=-230 and O/U=7.5, neither path fires → no stack unlocked."""
        pool = [
            _make_candidate(name="SP_0", team="BOS", is_pitcher=True, game_id=1, total_score=80, env_score=0.7),
            _make_candidate(name="NYY_1", team="NYY", is_pitcher=False, game_id=2, total_score=75, env_score=0.95),
            _make_candidate(name="NYY_2", team="NYY", is_pitcher=False, game_id=2, total_score=72, env_score=0.93),
            _make_candidate(name="LAD_1", team="LAD", is_pitcher=False, game_id=3, total_score=55, env_score=0.6),
            _make_candidate(name="HOU_1", team="HOU", is_pitcher=False, game_id=4, total_score=50, env_score=0.55),
            _make_candidate(name="SF_1", team="SF", is_pitcher=False, game_id=5, total_score=48, env_score=0.5),
        ]
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        # Blowout ML but low total — should NOT unlock stacking.
        slate = _stack_eligible_slate(favored_team="NYY", moneyline=-230, vegas_total=7.5)
        lineup = _enforce_composition(pool, slate)
        from collections import Counter
        nyy_count = Counter(c.team for c in lineup if not c.is_pitcher)["NYY"]
        assert nyy_count <= MAX_PLAYERS_PER_TEAM_BATTERS_DEFAULT, (
            f"Low-O/U game should not unlock NYY stack, got {nyy_count}"
        )

    def test_no_opposing_batter_in_anchor_game(self):
        """V10.1: the only game-level rule — never draft an opposing batter against our anchor."""
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        pitcher = next(c for c in lineup if c.is_pitcher)
        anchor_team = pitcher.team.upper()
        for b in lineup:
            if b.is_pitcher:
                continue
            if b.game_id == pitcher.game_id:
                assert b.team.upper() == anchor_team, (
                    f"Opposing batter {b.player_name} ({b.team}) in anchor's game"
                )

    def test_anchor_teammate_allowed_when_stack_eligible(self):
        """V10.1: anchor + teammate-stack in a stack-eligible game is permitted."""
        pool = [
            _make_candidate(name="SP_0", team="NYY", is_pitcher=True, game_id=1, total_score=80, env_score=0.8),
            _make_candidate(name="Teammate_1", team="NYY", is_pitcher=False, game_id=1, total_score=70, env_score=0.9),
            _make_candidate(name="Bat_Bos", team="BOS", is_pitcher=False, game_id=2, total_score=60, env_score=0.8),
            _make_candidate(name="Bat_Lad", team="LAD", is_pitcher=False, game_id=3, total_score=55, env_score=0.75),
            _make_candidate(name="Bat_Hou", team="HOU", is_pitcher=False, game_id=4, total_score=50, env_score=0.7),
        ]
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        slate = _stack_eligible_slate(favored_team="NYY")
        lineup = _enforce_composition(pool, slate)
        names = [c.player_name for c in lineup]
        assert "Teammate_1" in names, "Stack-eligible anchor teammate should be allowed"

    def test_no_pitcher_returns_pure_batter_lineup(self):
        """V10.5: a pitcher-free pool now produces a 5-batter lineup
        (the pure-batter shootout shape) instead of raising."""
        batters_only = [
            _make_candidate(name=f"B{i}", team=t, game_id=i)
            for i, t in enumerate(["NYY", "BOS", "LAD", "HOU", "ATL"])
        ]
        for c in batters_only:
            c.filter_ev = 50.0
        lineup = _enforce_composition(batters_only, _default_slate())
        assert len(lineup) == 5
        assert sum(1 for c in lineup if c.is_pitcher) == 0

    def test_too_few_batters_no_pitcher_raises(self):
        """If there's no pitcher AND fewer than 5 batters, neither variant works."""
        from app.services.filter_strategy import _enforce_composition
        too_few = [
            _make_candidate(name=f"B{i}", team=t, game_id=i)
            for i, t in enumerate(["NYY", "BOS", "LAD"])
        ]
        for c in too_few:
            c.filter_ev = 50.0
        with pytest.raises(ValueError):
            _enforce_composition(too_few, _default_slate())

    def test_pure_batter_wins_when_batter_evs_dominate(self):
        """When the top batter EVs vastly exceed the best pitcher's EV, the
        EV-driven chooser should pick 0P+5B.  This is the V10.5 behavior that
        unlocks the 4-of-5-winners-yesterday shootout shape."""
        # Single weak pitcher
        weak_pitcher = _make_candidate(
            "WeakP", is_pitcher=True, env_score=0.4, total_score=30,
            game_id=99, team="WSH",
        )
        # 5 strong batters on different teams
        strong_batters = [
            _make_candidate(
                name=f"StrongB{i}", team=t, game_id=i,
                env_score=0.9, total_score=85,
            )
            for i, t in enumerate(["NYY", "BOS", "LAD", "HOU", "ATL"])
        ]
        pool = [weak_pitcher] + strong_batters
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        assert len(lineup) == 5
        assert sum(1 for c in lineup if c.is_pitcher) == 0, (
            f"Expected pure-batter lineup; got {[(c.player_name, c.is_pitcher) for c in lineup]}"
        )

    def test_pitcher_wins_when_anchor_ev_dominates(self):
        """When the best pitcher has a strong EV edge over the marginal batter,
        the 1P+4B shape should win.  Sanity check that V10.5 doesn't regress."""
        strong_pitcher = _make_candidate(
            "AcePitcher", is_pitcher=True, env_score=0.95, total_score=95,
            game_id=99, team="WSH",
        )
        weak_batters = [
            _make_candidate(
                name=f"WeakB{i}", team=t, game_id=i,
                env_score=0.30, total_score=20,
            )
            for i, t in enumerate(["NYY", "BOS", "LAD", "HOU", "ATL"])
        ]
        pool = [strong_pitcher] + weak_batters
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        assert len(lineup) == 5
        assert sum(1 for c in lineup if c.is_pitcher) == 1
        assert lineup[0].player_name == "AcePitcher"


# ===================================================================
# 10. Slot Assignment
# ===================================================================

class TestSlotAssignment:
    def test_slot_1_is_highest_ev_player(self):
        """V12: slot 1 (2.0× multiplier) goes to the highest-EV player —
        not necessarily a pitcher.  Rearrangement inequality: best player
        in best slot."""
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        slots = _smart_slot_assignment(lineup)
        slot1 = next(s for s in slots if s.slot_index == PITCHER_ANCHOR_SLOT)
        # Slot 1 candidate must have the highest filter_ev among the lineup
        max_ev = max(c.filter_ev for c in lineup)
        assert slot1.candidate.filter_ev == pytest.approx(max_ev, abs=0.001)

    def test_5_slots_assigned(self):
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        slots = _smart_slot_assignment(lineup)
        assert len(slots) == 5
        assert {s.slot_index for s in slots} == {1, 2, 3, 4, 5}

    def test_slot_multipliers_correct(self):
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_base_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        slots = _smart_slot_assignment(lineup)
        for s in slots:
            assert s.slot_mult == SLOT_MULTIPLIERS[s.slot_index]

    def test_empty_candidates(self):
        slots = _smart_slot_assignment([])
        assert slots == []


# ===================================================================
# 11. Full Pipeline: run_filter_strategy
# ===================================================================

class TestRunFilterStrategy:
    def test_produces_valid_lineup(self):
        """V12: any pitcher count 0..5 is legal; chooser picks best by EV."""
        pool = _make_pool()
        result = run_filter_strategy(pool, _default_slate())
        assert isinstance(result, FilterOptimizedLineup)
        assert len(result.slots) == 5
        assert result.total_expected_value > 0
        # Total players = 5
        assert result.composition["pitchers"] + result.composition["hitters"] == 5

    def test_empty_candidates(self):
        result = run_filter_strategy([], _default_slate())
        assert result.slots == []
        assert result.total_expected_value == 0.0

    def test_pitcher_only_pool_yields_5p_lineup(self):
        """V12: when only pitchers are available, the chooser picks the 5P+0B variant."""
        pool = []
        for i in range(6):
            c = _make_candidate(name=f"P{i}", team=f"T{i}", is_pitcher=True,
                                game_id=i, env_score=0.85, total_score=70)
            pool.append(c)
        result = run_filter_strategy(pool, _default_slate())
        assert result.composition["pitchers"] == 5
        assert result.composition["hitters"] == 0

    def test_anti_correlation_guard_blocks_opposing_batter(self):
        """V12: a high-EV opposing batter must NOT be drafted alongside our pitcher."""
        pool = [
            _make_candidate(name="ACE", team="NYY", is_pitcher=True,
                            game_id=1, env_score=1.0, total_score=95),
            _make_candidate(name="OPP_BAT", team="BOS", is_pitcher=False,
                            game_id=1, env_score=0.99, total_score=95, batting_order=1),
            _make_candidate(name="TEAMMATE", team="NYY", is_pitcher=False,
                            game_id=1, env_score=0.85, total_score=80, batting_order=2),
        ]
        for i in range(5):
            pool.append(_make_candidate(
                name=f"OTHER_{i}", team=f"T{10+i}", game_id=10+i,
                env_score=0.6, total_score=55, batting_order=4,
            ))
        # Pre-set filter_ev so ACE wins (forces 1P+4B variant)
        for c in pool:
            c.filter_ev = (200.0 if c.player_name == "ACE"
                           else 150.0 if c.player_name == "OPP_BAT"
                           else 130.0 if c.player_name == "TEAMMATE"
                           else 80.0)
        result = run_filter_strategy(pool, _default_slate())
        names = {s.candidate.player_name for s in result.slots}
        # Our pitcher should be in (highest EV by design)
        assert "ACE" in names
        # Opposing batter MUST be blocked even though their EV (150) beats every "other"
        assert "OPP_BAT" not in names, "Anti-correlation guard failed"
        # Teammate is allowed
        assert "TEAMMATE" in names


