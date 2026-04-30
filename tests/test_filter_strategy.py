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
    _compute_filter_ev,
    _enforce_composition,
    _smart_slot_assignment,
    run_filter_strategy,
)
from app.core.constants import (
    REQUIRED_PITCHERS_IN_LINEUP,
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
            opp_starter_whip=1.40,
            opp_starter_k_per_9=6.0,        # V10.6: contact pitcher → full K/9 credit (0.4)
        )
        # Group A run_env: 4 main signals at 1.0 + WHIP at 0.5 weight + K/9 at 0.4 weight = 4.9 raw.
        #   Soft cap: first 2.0 taken full, excess 2.9 at 25% slope → 2.0 + 0.725 = 2.725
        # Group B situation: platoon 1.0 + order 2 → 1.0 = 2.0
        # Group C venue: COL (1.38) → 1.0
        # Group D (momentum): NEUTRALISED in V10.7 → 0.0 (always)
        # Total = 2.725 + 2.0 + 1.0 = 5.725
        # V10.7: BATTER_ENV_MAX_SCORE dropped to 5.0 to match the new max
        # (Group D removed).  5.725 / 5.0 = 1.145 → clamped to 1.0 by the
        # final `min(1.0, total/max)` step.  Perfect-storm matchups now
        # saturate, which is the correct behaviour now that the inverted
        # momentum signals don't artificially inflate the denominator.
        assert score == pytest.approx(1.0, abs=0.01)
        assert unknown == 0

    def test_empty_env_tracks_unknowns(self):
        score, factors, unknown = compute_batter_env_score()
        # All inputs None: no signal contributes → score = 0.0
        assert score == pytest.approx(0.0, abs=0.01)
        # vegas_total, opp_pitcher_era, batting_order, team_moneyline, opp_bullpen_era,
        # opp_starter_whip, opp_starter_k_per_9 = 7 unknowns (V10.6 added K/9 as A6).
        assert unknown == 7

    def test_correlated_signal_cap(self):
        """Run-env signals (O/U, ERA, ML, bullpen) compressed via soft cap.

        Soft cap: first 2.0 of sum at full value, excess at 25% slope.  V10.8
        Vegas O/U dropped from weight 1.0 to 0.5 after the audit showed it's a
        flat signal, so the 4-signal raw sum is now 0.5 (O/U) + 1.0 (ERA) +
        1.0 (ML) + 1.0 (bullpen) = 3.5 (vs 4.0 pre-V10.8).  Soft cap takes
        2.0 full + 1.5 × 0.25 = 2.375 (was 2.5).  Still preserves the anti-
        redundancy spirit — naive sum would be 4× the 2-signal case, soft-
        capped result is closer to ~1.4×.
        """
        # All 4 run-env signals maxed = 3.5 raw (V10.8) → soft-capped to 2.375
        score_all, _, _ = compute_batter_env_score(
            vegas_total=10.0, opp_pitcher_era=5.5,
            team_moneyline=-250, opp_bullpen_era=5.5,
            batting_order=5,
        )
        # Only 2 run-env signals maxed = 1.5 (V10.8: O/U weight 0.5 + ERA 1.0).
        # Below soft-cap point so taken at full value.
        score_two, _, _ = compute_batter_env_score(
            vegas_total=10.0, opp_pitcher_era=5.5,
            batting_order=5,
        )
        # Soft cap keeps the 4-signal case from being a naive 4× the 2-signal
        # case; the gap is ~0.175 in env_score units (about 1.4× the 2-signal).
        # Threshold = 0.20 leaves headroom for future Group A weight changes
        # without false alarms; the assertion still catches a regression that
        # would let redundant signals multiply linearly.
        assert score_all > score_two, "soft cap still rewards extra maxed signals"
        assert score_all - score_two < 0.20, "soft cap prevents redundant multiplication"

    def test_max_score_denominator_matches_constant(self):
        """max_score (BATTER_ENV_MAX_SCORE) is the env-score denominator.
        V10.7 dropped it from 6.0 → 5.0 because Group D (series + L10) was
        neutralised after the fresh-eyes audit revealed those signals were
        inverted at the player level.

        This test calls compute_batter_env_score with K/9 absent, so total
        without K/9 = 2.625 (Group A with WHIP at full saturation) +
        2.0 (situation) + 1.0 (venue) + 0.0 (Group D) = 5.625 raw.
        Divided by BATTER_ENV_MAX_SCORE=5.0 → 1.125 → clamped to 1.0.
        """
        from app.core.constants import BATTER_ENV_MAX_SCORE
        score, _, unknown = compute_batter_env_score(
            vegas_total=10.0,
            opp_pitcher_era=5.5,
            platoon_advantage=True,
            batting_order=2,
            park_team="COL",
            team_moneyline=-250,
            opp_bullpen_era=5.5,
            opp_starter_whip=1.40,
        )
        # Confirm BATTER_ENV_MAX_SCORE matches the V10.7 expected value (5.0)
        # so a future re-bump catches a regression here.
        assert BATTER_ENV_MAX_SCORE == 5.0
        # Perfect-storm matchups saturate at 1.0 in V10.7.
        assert score == pytest.approx(1.0, abs=0.01)

    def test_batting_order_unknown_contributes_zero(self):
        """Unknown batting order contributes 0 to situation — DNP risk is
        handled separately by _compute_dnp_adjustment (single multiplier),
        so env scoring stays faithful to actual pre-game signals."""
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
        # Unknown (0 contribution) should be strictly less than any confirmed order.
        assert score_unknown < score_known
        # Non-zero because Vegas+ERA still contribute to Group A run_env.
        assert score_unknown > 0, "Group A signals carry the env score when order is unknown"
        # batting_order + team_moneyline + opp_bullpen_era + opp_starter_whip +
        # opp_starter_k_per_9 = 5 unknowns (V10.6: K/9 added as Group A A6)
        assert unknown == 5

    def test_graduated_batting_order(self):
        """V8.0: batting order uses graduated scale, not hard top-5 gate."""
        score_3, _, _ = compute_batter_env_score(batting_order=3)  # 1.0
        score_5, _, _ = compute_batter_env_score(batting_order=5)  # 0.75
        score_7, _, _ = compute_batter_env_score(batting_order=7)  # 0.50
        score_9, _, _ = compute_batter_env_score(batting_order=9)  # 0.25
        assert score_3 > score_5 > score_7 > score_9 > 0

    def test_opp_starter_whip_signal(self):
        """V10.3: opposing starter WHIP is a Group A A5 factor at 0.5 weight."""
        # Vulnerable WHIP (≥1.40) should produce higher env than elite WHIP (<1.10)
        # holding all other signals identical.
        score_vuln, _, _ = compute_batter_env_score(
            vegas_total=8.0,                  # below floor → 0 contribution from O/U
            opp_pitcher_era=3.0,              # below floor → 0 contribution from ERA
            opp_starter_whip=1.40,            # ceiling → full WHIP contribution (0.5)
        )
        score_elite, _, _ = compute_batter_env_score(
            vegas_total=8.0,
            opp_pitcher_era=3.0,
            opp_starter_whip=1.05,            # below floor → 0 contribution
        )
        score_unknown, _, _ = compute_batter_env_score(
            vegas_total=8.0,
            opp_pitcher_era=3.0,
            opp_starter_whip=None,
        )
        # Vulnerable WHIP > elite WHIP (positive signal)
        assert score_vuln > score_elite
        # Elite WHIP and unknown both contribute zero, so they should be equal
        assert score_elite == pytest.approx(score_unknown, abs=0.001)

    def test_opp_starter_whip_unknown_increments_count(self):
        """V10.3: WHIP None bumps unknown_count, like other Group A signals."""
        _, _, unknown_with_whip = compute_batter_env_score(
            vegas_total=9.5,
            opp_pitcher_era=5.0,
            batting_order=3,
            team_moneyline=-200,
            opp_bullpen_era=4.5,
            opp_starter_whip=1.30,
        )
        _, _, unknown_without_whip = compute_batter_env_score(
            vegas_total=9.5,
            opp_pitcher_era=5.0,
            batting_order=3,
            team_moneyline=-200,
            opp_bullpen_era=4.5,
            # opp_starter_whip omitted (None)
        )
        assert unknown_without_whip == unknown_with_whip + 1

    def test_wind_in_penalty(self):
        """V10.3: wind blowing IN penalises venue, mirroring the OUT bonus."""
        # Neutral park (CLE pf=1.00) starts venue at 0.5 — leaves headroom for OUT
        # to add and IN to subtract without saturating against the 0/1.0 bounds.
        score_out, _, _ = compute_batter_env_score(
            park_team="CLE",
            wind_speed_mph=15,
            wind_direction="OUT TO CF",
        )
        score_in, _, _ = compute_batter_env_score(
            park_team="CLE",
            wind_speed_mph=15,
            wind_direction="IN FROM CF",
        )
        score_neutral, _, _ = compute_batter_env_score(
            park_team="CLE",
            wind_speed_mph=15,
            wind_direction="WEST",  # cross-wind, no IN/OUT match
        )
        assert score_out > score_neutral > score_in
        # Penalty floors venue at 0.0 — should never go negative.
        assert score_in >= 0.0

    def test_wind_in_below_speed_min_no_penalty(self):
        """Wind IN below the speed minimum should NOT penalise (consistent with OUT)."""
        score_in_calm, _, _ = compute_batter_env_score(
            park_team="CLE",
            wind_speed_mph=5,                 # below BATTER_ENV_WIND_SPEED_MIN (10)
            wind_direction="IN FROM CF",
        )
        score_neutral, _, _ = compute_batter_env_score(
            park_team="CLE",
            wind_speed_mph=5,
            wind_direction="WEST",
        )
        assert score_in_calm == pytest.approx(score_neutral, abs=0.001)

    def test_v10_4_mild_favorite_gets_ml_credit(self):
        """V10.4: mild favorites (-110 to -180) get partial-to-full ML credit
        for batters.  Pre-V10.4 the band was -130 to -220, so a -120 mild-fav
        team got ZERO ML contribution — but the 33-slate analysis shows
        mild favorites concentrate HV (1.32 HV/game vs 1.14 for -200 favs)."""
        score_mild, _, _ = compute_batter_env_score(
            vegas_total=8.0,
            opp_pitcher_era=4.0,
            team_moneyline=-120,    # mild favorite — peak HV bucket per data
        )
        score_pickem, _, _ = compute_batter_env_score(
            vegas_total=8.0,
            opp_pitcher_era=4.0,
            team_moneyline=-100,    # at floor — no ML credit
        )
        # Mild fav now meaningfully outscores a pickem game (was equal before).
        assert score_mild > score_pickem

    def test_v10_4_batter_ml_saturates_at_180(self):
        """V10.4: ML at -180 saturates (full credit); going to -250 doesn't add more.
        This prevents heavy-favorite batters from being over-rewarded."""
        score_180, _, _ = compute_batter_env_score(
            vegas_total=8.0,
            opp_pitcher_era=4.0,
            team_moneyline=-180,
        )
        score_250, _, _ = compute_batter_env_score(
            vegas_total=8.0,
            opp_pitcher_era=4.0,
            team_moneyline=-250,
        )
        # Both saturate at the ceiling — heavy favorite gets no extra ML credit.
        assert score_180 == pytest.approx(score_250, abs=0.001)


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
        assert _compute_filter_ev(c) > 0

    def test_filter_ev_matches_base_ev(self):
        """V11.0: filter EV is the base EV.  No popularity bonus or penalty."""
        c = _make_candidate(env_score=0.7, total_score=65)
        assert _compute_filter_ev(c) == pytest.approx(_compute_base_ev(c), rel=1e-9)


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

    def test_default_slate_caps_batters_at_one_per_team(self):
        """V10.1: with no stack-eligible games, every team is capped at 1 batter."""
        pool = _make_pool()
        for c in pool:
            c.filter_ev = _compute_filter_ev(c)
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
            c.filter_ev = _compute_filter_ev(c)
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
            c.filter_ev = _compute_filter_ev(c)
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
            c.filter_ev = _compute_filter_ev(c)
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
            c.filter_ev = _compute_filter_ev(c)
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
            c.filter_ev = _compute_filter_ev(c)
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
            c.filter_ev = _compute_filter_ev(c)
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
            c.filter_ev = _compute_filter_ev(c)
        lineup = _enforce_composition(pool, _default_slate())
        assert len(lineup) == 5
        assert sum(1 for c in lineup if c.is_pitcher) == 1
        assert lineup[0].player_name == "AcePitcher"


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


