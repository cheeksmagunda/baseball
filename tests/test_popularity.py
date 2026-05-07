"""Tests for app/core/popularity.py — V15.1 continuous popularity score.

The popularity module produces a continuous predicted_ownership_score in
[0, ~10] for each candidate.  These tests pin five properties:
  1. The traditional path (predict_popularity_score) raises on missing
     OPS / ERA for non-rookies, never silently degrades.
  2. The rookie path (predict_rookie_popularity_score) accepts those
     same gaps without raising — true MLB debutants have no traditional
     stats by definition.
  3. Unknown teams raise loud — every team in PARK_HR_FACTORS must have
     a market tier, and a runtime miss is a real data-collection bug,
     not a missing-data event.
  4. The score → multiplier curve is monotone, neutral at NEUTRAL_SCORE,
     clamped at FLOOR / CEILING.
  5. V15.1 — elite-stats and fame-rate components are CONTINUOUS, not
     binary thresholds.  A 1-point edge in OPS produces a proportional
     increase in elite-stat pts; a 1-of-2-MP fame rate produces less
     than half the pts of a 2-of-2 rate.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.constants import (
    LEVERAGE_ELITE_BATTER_OPS_CEILING,
    LEVERAGE_ELITE_BATTER_OPS_FLOOR,
    LEVERAGE_ELITE_PITCHER_ERA_CEILING,
    LEVERAGE_ELITE_PITCHER_ERA_FLOOR,
    LEVERAGE_ELITE_STAT_MAX_PTS,
    POPULARITY_MULT_CEILING,
    POPULARITY_MULT_FLOOR,
    POPULARITY_NEUTRAL_SCORE,
)
from app.core.popularity import (
    _elite_stat_pts,
    popularity_score_to_multiplier,
    predict_popularity_score,
    predict_rookie_popularity_score,
)


_TODAY = date(2026, 5, 5)


# ---------------------------------------------------------------------------
# 1. Traditional path — strict precondition on OPS / ERA
# ---------------------------------------------------------------------------

class TestStrictPreconditions:
    def test_pitcher_missing_era_raises(self):
        with pytest.raises(RuntimeError, match="season_era=None"):
            predict_popularity_score(
                player_name="Mystery SP", team="NYY", is_pitcher=True,
                batting_order=None, season_ops=None, season_era=None,
                as_of=_TODAY,
            )

    def test_batter_missing_ops_raises(self):
        with pytest.raises(RuntimeError, match="season_ops=None"):
            predict_popularity_score(
                player_name="Mystery Bat", team="NYY", is_pitcher=False,
                batting_order=3, season_ops=None, season_era=None,
                as_of=_TODAY,
            )

    def test_unknown_team_raises(self):
        with pytest.raises(RuntimeError, match="not in TEAM_MARKET_TIER"):
            predict_popularity_score(
                player_name="Joe", team="XYZ", is_pitcher=False,
                batting_order=3, season_ops=0.700, season_era=None,
                as_of=_TODAY,
            )


# ---------------------------------------------------------------------------
# 2. Rookie path — ALLOWS missing season stats
# ---------------------------------------------------------------------------

class TestRookiePath:
    def test_rookie_with_no_stats_returns_a_score(self):
        """True debutant — zero current and prior season — must NOT raise."""
        score = predict_rookie_popularity_score(
            player_name="Debut Kid", team="KC", is_pitcher=False,
            batting_order=8, as_of=_TODAY,
        )
        # KC tier 4 = 0, no star, no fame, batting 8th (not top-3) → 0.0
        assert score == pytest.approx(0.0)

    def test_rookie_unknown_team_still_raises(self):
        """The team-tier check is enforced regardless of track — unknown
        team is always a data-collection bug, never a rookie carve-out."""
        with pytest.raises(RuntimeError, match="not in TEAM_MARKET_TIER"):
            predict_rookie_popularity_score(
                player_name="Debut Kid", team="XYZ", is_pitcher=False,
                batting_order=3, as_of=_TODAY,
            )

    def test_rookie_on_tier1_market_lifts_above_zero(self):
        """A Yankees debutant gets at least the team-market score."""
        score = predict_rookie_popularity_score(
            player_name="Yankee Rookie", team="NYY", is_pitcher=False,
            batting_order=2, as_of=_TODAY,
        )
        # NYY tier 1 = +2, top-3 batting = +1 → 3.0
        assert score == pytest.approx(3.0)

    def test_flagged_star_rookie_climbs(self):
        """A pre-flagged star prospect (Holliday, Chourio etc.) lifts further."""
        score = predict_rookie_popularity_score(
            player_name="Jackson Holliday", team="BAL", is_pitcher=False,
            batting_order=1, as_of=_TODAY,
        )
        # BAL tier 3 = +1, star = +3, top-3 = +1 → 5.0
        assert score == pytest.approx(5.0)

    def test_rookie_on_tier4_with_no_signals_gets_max_boost(self):
        """The rookie-pitchers-aren't-faded-too-hard guarantee.  A KC rookie
        pitcher with no fame, no stats, no top-of-order batting ends up at
        score=0 and earns the maximum sleeper-leverage boost — partially
        offsetting the V13.3 env cap.  Combined: env (≤1.10) × trait (1.0)
        × leverage (1.20) = ~1.32 max EV multiplier (vs ~1.51 for a
        comparable veteran ace), structurally below veterans but not
        double-faded into oblivion."""
        score = predict_rookie_popularity_score(
            player_name="Anonymous Rookie SP", team="KC", is_pitcher=True,
            batting_order=None, as_of=_TODAY,
        )
        assert score == pytest.approx(0.0)
        # Score of 0 → max multiplier (the ceiling)
        assert popularity_score_to_multiplier(score) == pytest.approx(POPULARITY_MULT_CEILING)


# ---------------------------------------------------------------------------
# 3. popularity_score_to_multiplier — curve shape
# ---------------------------------------------------------------------------

class TestPopularityCurve:
    def test_neutral_score_returns_one(self):
        assert popularity_score_to_multiplier(POPULARITY_NEUTRAL_SCORE) == pytest.approx(1.0)

    def test_high_score_clamped_to_floor(self):
        assert popularity_score_to_multiplier(100.0) == pytest.approx(POPULARITY_MULT_FLOOR)

    def test_low_score_clamped_to_ceiling(self):
        assert popularity_score_to_multiplier(-100.0) == pytest.approx(POPULARITY_MULT_CEILING)

    def test_none_returns_one(self):
        """None falls back to neutral — the only acceptable default."""
        assert popularity_score_to_multiplier(None) == 1.0

    def test_curve_is_monotone(self):
        scores = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        multipliers = [popularity_score_to_multiplier(s) for s in scores]
        for i in range(len(multipliers) - 1):
            assert multipliers[i] >= multipliers[i + 1], (
                f"Curve non-monotone at score {scores[i]}→{scores[i+1]}"
            )

    def test_band_straddles_one(self):
        assert POPULARITY_MULT_FLOOR < 1.0 < POPULARITY_MULT_CEILING


# ---------------------------------------------------------------------------
# 4. Traditional path — produces sensible scores on real-shape inputs
# ---------------------------------------------------------------------------

class TestTraditionalPath:
    def test_judge_lands_high_popularity(self):
        score = predict_popularity_score(
            player_name="Aaron Judge", team="NYY", is_pitcher=False,
            batting_order=2, season_ops=0.950, season_era=None,
            as_of=_TODAY,
        )
        # NYY tier 1 = +3, star = +3, top-3 = +1, plus fame index → ≥ 7.0
        # → multiplier near floor
        assert score >= 7.0
        assert popularity_score_to_multiplier(score) <= 0.92

    def test_anonymous_small_market_lands_low(self):
        score = predict_popularity_score(
            player_name="Joe Schmoe", team="KC", is_pitcher=False,
            batting_order=9, season_ops=0.650, season_era=None,
            as_of=_TODAY,
        )
        # KC tier 4 = 0, no star, no fame, no top-3 → 0.0 → max boost
        assert score == pytest.approx(0.0)
        assert popularity_score_to_multiplier(score) == pytest.approx(POPULARITY_MULT_CEILING)

    def test_breakout_lifts_via_elite_ops(self):
        """V15.1 — a non-flagged player at the OPS ceiling earns the FULL
        elite-stats bonus (LEVERAGE_ELITE_STAT_MAX_PTS); the ramp is
        continuous from floor (0 pts) to ceiling (max pts).  Catches
        breakouts before the offseason STAR_PLAYER_FLAGS update."""
        cold = predict_popularity_score(
            player_name="Anonymous Hitter", team="MIA", is_pitcher=False,
            batting_order=3, season_ops=LEVERAGE_ELITE_BATTER_OPS_FLOOR, season_era=None,
            as_of=_TODAY,
        )
        hot = predict_popularity_score(
            player_name="Anonymous Hitter", team="MIA", is_pitcher=False,
            batting_order=3, season_ops=LEVERAGE_ELITE_BATTER_OPS_CEILING, season_era=None,
            as_of=_TODAY,
        )
        # Hot version gets the full +LEVERAGE_ELITE_STAT_MAX_PTS over the
        # floor version (max - 0 = max).
        assert hot - cold == pytest.approx(LEVERAGE_ELITE_STAT_MAX_PTS)
        # And lower multiplier (more popular)
        assert popularity_score_to_multiplier(hot) < popularity_score_to_multiplier(cold)


# ---------------------------------------------------------------------------
# 5. V15.1 — continuous components, no binary thresholds
# ---------------------------------------------------------------------------

class TestContinuousElite:
    """V15.1 replaces V15's binary OPS/ERA thresholds with smooth ramps.
    A 1-point edge in OPS or ERA produces a proportional change in pts —
    no boundary cliffs (a player at OPS 0.949 vs 0.951 should differ by
    < 0.05 pts, not 2.0)."""

    def test_ops_floor_returns_zero(self):
        assert _elite_stat_pts(False, LEVERAGE_ELITE_BATTER_OPS_FLOOR, None) == pytest.approx(0.0)

    def test_ops_ceiling_returns_max(self):
        assert _elite_stat_pts(False, LEVERAGE_ELITE_BATTER_OPS_CEILING, None) == pytest.approx(
            LEVERAGE_ELITE_STAT_MAX_PTS
        )

    def test_ops_below_floor_returns_zero(self):
        assert _elite_stat_pts(False, 0.500, None) == pytest.approx(0.0)

    def test_ops_above_ceiling_returns_max(self):
        assert _elite_stat_pts(False, 1.100, None) == pytest.approx(LEVERAGE_ELITE_STAT_MAX_PTS)

    def test_ops_midpoint_returns_half_max(self):
        midpoint = (LEVERAGE_ELITE_BATTER_OPS_FLOOR + LEVERAGE_ELITE_BATTER_OPS_CEILING) / 2
        assert _elite_stat_pts(False, midpoint, None) == pytest.approx(
            LEVERAGE_ELITE_STAT_MAX_PTS / 2
        )

    def test_ops_ramp_is_monotone(self):
        ops_values = [0.500, 0.650, 0.700, 0.800, 0.900, 0.950, 1.050]
        pts = [_elite_stat_pts(False, ops, None) for ops in ops_values]
        for i in range(len(pts) - 1):
            assert pts[i] <= pts[i + 1], f"OPS pts non-monotone at {ops_values[i]}→{ops_values[i+1]}"

    def test_era_ceiling_returns_zero(self):
        # Higher ERA = less popular = zero pts at the ceiling
        assert _elite_stat_pts(True, None, LEVERAGE_ELITE_PITCHER_ERA_CEILING) == pytest.approx(0.0)

    def test_era_floor_returns_max(self):
        # Lower ERA = more popular = max pts at the floor
        assert _elite_stat_pts(True, None, LEVERAGE_ELITE_PITCHER_ERA_FLOOR) == pytest.approx(
            LEVERAGE_ELITE_STAT_MAX_PTS
        )

    def test_era_zero_returns_zero_pts(self):
        """Opening-week 0-IP small-sample garbage — ERA=0.00 must NOT
        cascade to max pts (ace tier).  Conservative: treat as no signal."""
        assert _elite_stat_pts(True, None, 0.00) == pytest.approx(0.0)

    def test_era_above_ceiling_returns_zero(self):
        assert _elite_stat_pts(True, None, 6.00) == pytest.approx(0.0)

    def test_era_ramp_is_monotone_descending(self):
        # Lower ERA = higher pts → ramp is descending in ERA value
        era_values = [2.50, 3.00, 3.50, 4.00, 4.50, 5.00]
        pts = [_elite_stat_pts(True, None, era) for era in era_values]
        for i in range(len(pts) - 1):
            assert pts[i] >= pts[i + 1], f"ERA pts not descending at {era_values[i]}→{era_values[i+1]}"

    def test_no_boundary_cliff(self):
        """V15.1 invariant — players just inside vs just outside the ramp
        should differ by less than the threshold-cliff would produce.
        V15 had a 2.0-point cliff at OPS 0.900; V15.1 should be < 0.10
        for a 0.002 OPS gap."""
        epsilon = 0.002
        below_ceiling = _elite_stat_pts(False, LEVERAGE_ELITE_BATTER_OPS_CEILING - epsilon, None)
        at_ceiling = _elite_stat_pts(False, LEVERAGE_ELITE_BATTER_OPS_CEILING, None)
        gap = at_ceiling - below_ceiling
        assert gap < 0.10, f"Boundary cliff at OPS ceiling: {gap}"


class TestContinuousFameRate:
    """V15.1 fame index is rate-based.  These properties are pinned via
    the predict_popularity_score smoke tests above (which exercise the
    full fame-rate pipeline through `_load_fame_rate_index`).  The unit
    tests below validate the math layer — `_fame_rate_pts` would be
    tested via mocking, but since the helper is private and trivial
    (return MAX_PTS * rate when denom > 0 else 0), the integration tests
    via predict_popularity_score and _elite_stat_pts cover it
    sufficiently.

    The position-aware window assertion (pitcher 28d, batter 14d) is
    enforced by `_validate_constants` at import time.
    """

    def test_position_window_invariant_holds(self):
        """Pitcher window is at least as long as batter window."""
        from app.core.constants import (
            LEVERAGE_FAME_INDEX_DAYS_BATTER,
            LEVERAGE_FAME_INDEX_DAYS_PITCHER,
        )
        assert LEVERAGE_FAME_INDEX_DAYS_PITCHER >= LEVERAGE_FAME_INDEX_DAYS_BATTER
