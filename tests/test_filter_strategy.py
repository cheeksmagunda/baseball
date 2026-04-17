"""
Tests for the active filter_strategy pipeline.

Covers: env score computation, slate classification, base EV computation,
FADE exclusion gate, composition enforcement, dual optimizer, and
popularity classification.
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
    _compute_filter_ev,
    _compute_moonshot_filter_ev,
    _exclude_fade_players,
    _enforce_composition,
    _smart_slot_assignment,
    run_filter_strategy,
    run_dual_filter_strategy,
)
from app.services.popularity import PopularityClass, classify_player
from app.core.constants import (
    REQUIRED_PITCHERS_IN_LINEUP,
    MAX_PLAYERS_PER_TEAM,
    MAX_PLAYERS_PER_GAME,
    PITCHER_ANCHOR_SLOT,
    SLOT_MULTIPLIERS,
    STACK_BONUS,
    DNP_RISK_PENALTY,
    DNP_UNKNOWN_PENALTY,
    ENV_UNKNOWN_COUNT_THRESHOLD,
)


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
    popularity: PopularityClass = PopularityClass.TARGET,
    game_id: int | str | None = 1,
    card_boost: float = 0.0,
    drafts: int | None = None,
    batting_order: int | None = 3,
    env_unknown_count: int = 0,
    is_in_blowout_game: bool = False,
    sharp_score: float = 0.0,
    traits: list | None = None,
) -> FilteredCandidate:
    return FilteredCandidate(
        player_name=name,
        team=team,
        position="SP" if is_pitcher else position,
        card_boost=card_boost,
        total_score=total_score,
        env_score=env_score,
        env_unknown_count=env_unknown_count,
        popularity=popularity,
        game_id=game_id,
        is_pitcher=is_pitcher,
        sharp_score=sharp_score,
        drafts=drafts,
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


def _make_pool(n_pitchers: int = 2, n_batters: int = 10) -> list[FilteredCandidate]:
    """Build a realistic candidate pool with distinct teams/games."""
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
            env_score=0.7,
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
        games = [{"home_moneyline": -220, "home_team": "NYY"}]
        result = classify_slate(5, games=games)
        assert result.slate_type == SlateType.HITTER_DAY
        assert result.blowout_games == 1
        assert result.stackable_games[0].favored_team == "NYY"

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
    def test_perfect_env(self):
        score, factors = compute_pitcher_env_score(
            opp_team_ops=0.650,
            opp_team_k_pct=0.26,
            pitcher_k_per_9=10.0,
            park_team="SF",  # PF=0.92 → graduated (1.05-0.92)/0.15 = 0.867
            is_home=True,
            team_moneyline=-250,
        )
        # 5 main factors + home (0.5), max_score = 5.5
        # SF park (0.92): (1.05-0.92)/0.15 = 0.867 (graduated, not 1.0)
        # Total ≈ 1.0 + 1.0 + 1.0 + 0.867 + 1.0 + 0.5 = 5.367 / 5.5 ≈ 0.976
        assert score > 0.9
        assert len(factors) >= 5

    def test_empty_env(self):
        score, factors = compute_pitcher_env_score()
        assert score == pytest.approx(0.0, abs=0.01)
        assert factors == []

    def test_home_field_only(self):
        score, factors = compute_pitcher_env_score(is_home=True)
        assert score > 0
        assert any("Home" in f for f in factors)

    def test_moneyline_adds_win_bonus(self):
        """V8.0: pitcher moneyline captures Win bonus probability."""
        score_no_ml, _ = compute_pitcher_env_score(
            pitcher_k_per_9=9.0, is_home=True,
        )
        score_heavy_fav, factors = compute_pitcher_env_score(
            pitcher_k_per_9=9.0, is_home=True, team_moneyline=-250,
        )
        assert score_heavy_fav > score_no_ml
        assert any("Win" in f or "Favorite" in f or "favorite" in f for f in factors)

    def test_max_score_denominator_is_5_5(self):
        """max_score = 5.5 (5 main factors at 1.0 each + home 0.5)."""
        score, _ = compute_pitcher_env_score(
            opp_team_ops=0.650,
            opp_team_k_pct=0.26,
            pitcher_k_per_9=10.0,
            park_team="LAD",  # PF=0.89 → full 1.0 (≤ 0.90)
            is_home=True,
            team_moneyline=-250,
        )
        # All 5 factors at 1.0 + home 0.5 = 5.5 / 5.5 = 1.0
        assert score == pytest.approx(1.0, abs=0.01), "All signals maxed should reach 1.0"

    def test_graduated_thresholds(self):
        """V8.0: thresholds use linear interpolation, not hard cliffs."""
        # K/9 of 8.0 should get partial credit (graduated 6→0, 10→1)
        score_k8, _ = compute_pitcher_env_score(pitcher_k_per_9=8.0)
        score_k10, _ = compute_pitcher_env_score(pitcher_k_per_9=10.0)
        assert 0 < score_k8 < score_k10, "K/9=8.0 should get partial, not full credit"


# ===================================================================
# 3. Batter Env Score
# ===================================================================

class TestBatterEnvScore:
    def test_perfect_env(self):
        score, factors, unknown = compute_batter_env_score(
            vegas_total=10.0,
            opp_pitcher_era=5.5,
            platoon_advantage=True,
            batting_order=2,
            park_team="COL",
            team_moneyline=-250,
            opp_bullpen_era=5.5,
        )
        # Group A run_env: 4 signals at 1.0, capped at 2.0
        # Group B situation: platoon 1.0 + order 2 → 1.0 = 2.0
        # Group C venue: COL (1.38) → 1.0
        # Group D (momentum): none provided → 0.0
        # Total = 2.0 + 2.0 + 1.0 = 5.0 / 5.8 (BATTER_ENV_MAX_SCORE) ≈ 0.862
        assert score == pytest.approx(5.0 / 5.8, abs=0.01)
        assert unknown == 0

    def test_empty_env_tracks_unknowns(self):
        score, factors, unknown = compute_batter_env_score()
        # All inputs None: no signal contributes → score = 0.0
        assert score == pytest.approx(0.0, abs=0.01)
        # vegas_total, opp_pitcher_era, batting_order, team_moneyline, opp_bullpen_era = 5 unknowns
        assert unknown == 5

    def test_correlated_signal_cap(self):
        """V8.0: run-env signals (O/U, ERA, ML, bullpen) capped at 2.0."""
        # All 4 run-env signals maxed = 4.0, but capped at 2.0
        score_all, _, _ = compute_batter_env_score(
            vegas_total=10.0, opp_pitcher_era=5.5,
            team_moneyline=-250, opp_bullpen_era=5.5,
            batting_order=5,
        )
        # Only 2 run-env signals
        score_two, _, _ = compute_batter_env_score(
            vegas_total=10.0, opp_pitcher_era=5.5,
            batting_order=5,
        )
        # Due to cap at 2.0, adding 2 more maxed signals shouldn't help much
        assert score_all == pytest.approx(score_two, abs=0.05)

    def test_max_score_denominator_is_5_8(self):
        """max_score = 5.8 (run_env cap 2.0 + situation 2.0 + venue 1.0 + momentum 0.8)."""
        score, _, unknown = compute_batter_env_score(
            vegas_total=10.0,
            opp_pitcher_era=5.5,
            platoon_advantage=True,
            batting_order=2,
            park_team="COL",
            team_moneyline=-250,
            opp_bullpen_era=5.5,
        )
        # Without Group D (momentum), total = 2.0+2.0+1.0 = 5.0 / 5.8 ≈ 0.862
        assert score < 1.0, "Without momentum context, score should be < 1.0"
        assert score == pytest.approx(5.0 / 5.8, abs=0.01)

    def test_batting_order_unknown_gets_baseline(self):
        """V8.0: unknown batting order gets 0.40 baseline, not 0."""
        score_unknown, _, unknown = compute_batter_env_score(
            vegas_total=9.0,
            opp_pitcher_era=5.0,
            batting_order=None,
        )
        score_known, _, _ = compute_batter_env_score(
            vegas_total=9.0,
            opp_pitcher_era=5.0,
            batting_order=7,  # middle of lineup = 0.50
        )
        # Unknown (0.40) should be slightly less than confirmed middle (0.50)
        assert score_unknown < score_known
        assert score_unknown > 0, "Unknown order should not zero out env score"
        # batting_order + team_moneyline + opp_bullpen_era = 3 unknowns
        assert unknown == 3

    def test_graduated_batting_order(self):
        """V8.0: batting order uses graduated scale, not hard top-5 gate."""
        score_3, _, _ = compute_batter_env_score(batting_order=3)  # 1.0
        score_5, _, _ = compute_batter_env_score(batting_order=5)  # 0.75
        score_7, _, _ = compute_batter_env_score(batting_order=7)  # 0.50
        score_9, _, _ = compute_batter_env_score(batting_order=9)  # 0.25
        assert score_3 > score_5 > score_7 > score_9 > 0


# ===================================================================
# 4. FADE Exclusion Gate (V9.0)
# ===================================================================

class TestFADEGate:
    def test_fade_players_excluded(self):
        pool = [
            _make_candidate("P1", is_pitcher=True, popularity=PopularityClass.TARGET),
            _make_candidate("B1", popularity=PopularityClass.FADE),
            _make_candidate("B2", popularity=PopularityClass.TARGET),
            _make_candidate("B3", popularity=PopularityClass.NEUTRAL),
        ]
        result = _exclude_fade_players(pool)
        names = [c.player_name for c in result]
        assert "B1" not in names
        assert len(result) == 3

    def test_no_fade_players_unchanged(self):
        pool = [
            _make_candidate("P1", is_pitcher=True, popularity=PopularityClass.TARGET),
            _make_candidate("B1", popularity=PopularityClass.TARGET),
        ]
        assert len(_exclude_fade_players(pool)) == 2

    def test_all_pitchers_fade_raises(self):
        pool = [
            _make_candidate("P1", is_pitcher=True, popularity=PopularityClass.FADE),
            _make_candidate("B1", popularity=PopularityClass.TARGET),
        ]
        with pytest.raises(ValueError, match="[Pp]itcher"):
            _exclude_fade_players(pool)


# ===================================================================
# 5. Popularity Classification (BUG 1 fix)
# ===================================================================

class TestPopularityClassification:
    def test_high_pop_high_perf_is_fade(self):
        cls, _ = classify_player(60.0, 70.0)
        assert cls == PopularityClass.FADE

    def test_high_pop_low_perf_is_fade(self):
        cls, _ = classify_player(60.0, 10.0)
        assert cls == PopularityClass.FADE

    def test_low_pop_high_perf_is_target(self):
        cls, _ = classify_player(10.0, 70.0)
        assert cls == PopularityClass.TARGET

    def test_low_pop_mid_perf_is_target(self):
        """BUG 1 fix: score 30 with low pop should be TARGET (threshold lowered to 25)."""
        cls, _ = classify_player(10.0, 30.0)
        assert cls == PopularityClass.TARGET

    def test_ghost_score_25_is_target(self):
        """Ghost player at score boundary (25) should be TARGET, not NEUTRAL."""
        cls, _ = classify_player(0.0, 25.0)
        assert cls == PopularityClass.TARGET

    def test_ghost_score_24_is_neutral(self):
        """Ghost player below threshold (24) stays NEUTRAL."""
        cls, _ = classify_player(0.0, 24.0)
        assert cls == PopularityClass.NEUTRAL

    def test_zero_pop_zero_perf_is_neutral(self):
        cls, _ = classify_player(0.0, 0.0)
        assert cls == PopularityClass.NEUTRAL


# ===================================================================
# 6. DNP Adjustment
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

    def test_popularity_not_in_base_ev(self):
        """V9.0: popularity is a gate only — TARGET and FADE get same base EV."""
        target_c = _make_candidate(popularity=PopularityClass.TARGET)
        neutral_c = _make_candidate(popularity=PopularityClass.NEUTRAL)
        assert _compute_base_ev(target_c) == pytest.approx(_compute_base_ev(neutral_c), rel=0.01)


# ===================================================================
# 8. Filter EV (Starting 5 vs Moonshot)
# ===================================================================

class TestFilterEV:
    def test_filter_ev_positive(self):
        c = _make_candidate(popularity=PopularityClass.TARGET)
        assert _compute_filter_ev(c) > 0

    def test_moonshot_sharp_score_raises_ev(self):
        no_sharp = _make_candidate(sharp_score=0)
        with_sharp = _make_candidate(sharp_score=100)
        ev_base = _compute_moonshot_filter_ev(no_sharp)
        ev_sharp = _compute_moonshot_filter_ev(with_sharp)
        assert ev_sharp > ev_base

    def test_moonshot_ev_above_s5_with_sharp(self):
        target = _make_candidate(popularity=PopularityClass.TARGET, sharp_score=50)
        ev_s5 = _compute_filter_ev(target)
        ev_moon = _compute_moonshot_filter_ev(target)
        assert ev_moon > ev_s5


# ===================================================================
# 9. Composition Enforcement
# ===================================================================

class TestComposition:
    def test_exactly_5_players(self):
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_filter_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        assert len(lineup) == 5

    def test_exactly_1_pitcher(self):
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_filter_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        pitcher_count = sum(1 for c in lineup if c.is_pitcher)
        assert pitcher_count == REQUIRED_PITCHERS_IN_LINEUP

    def test_team_diversification(self):
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_filter_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        teams = [c.team for c in lineup]
        for team in set(teams):
            assert teams.count(team) <= MAX_PLAYERS_PER_TEAM

    def test_game_diversification(self):
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_filter_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        game_ids = [c.game_id for c in lineup if c.game_id is not None]
        for gid in set(game_ids):
            assert game_ids.count(gid) <= MAX_PLAYERS_PER_GAME

    def test_pitcher_game_blocked_for_batters(self):
        """No batter should share the anchor pitcher's game."""
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_filter_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        pitcher = next(c for c in lineup if c.is_pitcher)
        batters = [c for c in lineup if not c.is_pitcher]
        for b in batters:
            assert b.game_id != pitcher.game_id, (
                f"Batter {b.player_name} shares game {b.game_id} with pitcher"
            )

    def test_no_pitcher_raises(self):
        batters_only = [
            _make_candidate(name=f"B{i}", team=t, game_id=i)
            for i, t in enumerate(["NYY", "BOS", "LAD", "HOU", "ATL"])
        ]
        for c in batters_only:
            c.filter_ev = 50.0
        with pytest.raises(ValueError, match="no pitcher"):
            _enforce_composition(batters_only, _default_slate())


# ===================================================================
# 10. Slot Assignment
# ===================================================================

class TestSlotAssignment:
    def test_pitcher_in_slot_1(self):
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_filter_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        slots = _smart_slot_assignment(lineup)
        slot1 = next(s for s in slots if s.slot_index == PITCHER_ANCHOR_SLOT)
        assert slot1.candidate.is_pitcher

    def test_5_slots_assigned(self):
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_filter_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        slots = _smart_slot_assignment(lineup)
        assert len(slots) == 5
        assert {s.slot_index for s in slots} == {1, 2, 3, 4, 5}

    def test_slot_multipliers_correct(self):
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_filter_ev(c)
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
        pool = _make_pool()
        result = run_filter_strategy(pool, _default_slate())
        assert isinstance(result, FilterOptimizedLineup)
        assert len(result.slots) == 5
        assert result.total_expected_value > 0
        assert result.composition["pitchers"] == 1
        assert result.composition["hitters"] == 4

    def test_empty_candidates(self):
        result = run_filter_strategy([], _default_slate())
        assert result.slots == []
        assert result.total_expected_value == 0.0


# ===================================================================
# 12. Dual Optimizer: Starting 5 + Moonshot
# ===================================================================

class TestDualOptimizer:
    def _big_pool(self) -> list[FilteredCandidate]:
        """Pool large enough for two full lineups (10+ unique teams/games)."""
        teams = ["NYY", "BOS", "LAD", "HOU", "ATL", "CHC", "SF", "SEA",
                 "MIN", "TB", "SD", "CLE", "PHI", "MIL"]
        pool = []
        for i in range(3):
            pool.append(_make_candidate(
                name=f"SP_{i}", team=teams[i], is_pitcher=True,
                game_id=200 + i, total_score=60 + i * 5, env_score=0.7,
            ))
        for i in range(12):
            pool.append(_make_candidate(
                name=f"BAT_{i}", team=teams[3 + (i % 11)],
                game_id=300 + i, total_score=40 + i * 2, env_score=0.55 + i * 0.02,
            ))
        return pool

    def test_no_player_overlap(self):
        pool = self._big_pool()
        result = run_dual_filter_strategy(pool, _default_slate())
        s5_names = {s.candidate.player_name for s in result.starting_5.slots}
        moon_names = {s.candidate.player_name for s in result.moonshot.slots}
        assert s5_names.isdisjoint(moon_names), (
            f"Overlap: {s5_names & moon_names}"
        )

    def test_both_lineups_have_5(self):
        pool = self._big_pool()
        result = run_dual_filter_strategy(pool, _default_slate())
        assert len(result.starting_5.slots) == 5
        assert len(result.moonshot.slots) == 5

    def test_both_lineups_have_1_pitcher(self):
        pool = self._big_pool()
        result = run_dual_filter_strategy(pool, _default_slate())
        assert result.starting_5.composition["pitchers"] == 1
        assert result.moonshot.composition["pitchers"] == 1

    def test_moonshot_strategy_label(self):
        pool = self._big_pool()
        result = run_dual_filter_strategy(pool, _default_slate())
        assert result.moonshot.strategy == "moonshot"
        assert result.starting_5.strategy == "filter_not_forecast"

    def test_minimal_pool_both_lineups_filled(self):
        """V9.0: moonshot uses same pool as S5 — both lineups fill from 5 players."""
        teams = ["NYY", "BOS", "LAD", "HOU", "ATL"]
        pool = [
            _make_candidate(name="SP_0", team=teams[0], is_pitcher=True, game_id=1),
        ] + [
            _make_candidate(name=f"B_{i}", team=teams[i + 1], game_id=10 + i)
            for i in range(4)
        ]
        result = run_dual_filter_strategy(pool, _default_slate())
        assert len(result.starting_5.slots) == 5
        assert len(result.moonshot.slots) == 5
