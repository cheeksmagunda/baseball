"""Tests for app/core/popularity.py — V14 leverage scoring.

The popularity module produces the predicted_ownership_bucket for each
candidate.  These tests pin three properties:
  1. The traditional path (predict_popularity_bucket) raises on missing
     OPS / ERA for non-rookies, never silently degrades.
  2. The rookie path (predict_rookie_popularity_bucket) accepts those
     same gaps without raising — true MLB debutants have no traditional
     stats by definition.
  3. Unknown teams raise loud — every team in PARK_HR_FACTORS must have
     a market tier, and a runtime miss is a real data-collection bug,
     not a missing-data event.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.popularity import (
    _bucket_from_score,
    predict_popularity_bucket,
    predict_rookie_popularity_bucket,
)


_TODAY = date(2026, 5, 5)


# ---------------------------------------------------------------------------
# 1. Traditional path — strict precondition on OPS / ERA
# ---------------------------------------------------------------------------

class TestStrictPreconditions:
    def test_pitcher_missing_era_raises(self):
        with pytest.raises(RuntimeError, match="season_era=None"):
            predict_popularity_bucket(
                player_name="Mystery SP", team="NYY", is_pitcher=True,
                batting_order=None, season_ops=None, season_era=None,
                as_of=_TODAY,
            )

    def test_batter_missing_ops_raises(self):
        with pytest.raises(RuntimeError, match="season_ops=None"):
            predict_popularity_bucket(
                player_name="Mystery Bat", team="NYY", is_pitcher=False,
                batting_order=3, season_ops=None, season_era=None,
                as_of=_TODAY,
            )

    def test_unknown_team_raises(self):
        with pytest.raises(RuntimeError, match="not in TEAM_MARKET_TIER"):
            predict_popularity_bucket(
                player_name="Joe", team="XYZ", is_pitcher=False,
                batting_order=3, season_ops=0.700, season_era=None,
                as_of=_TODAY,
            )


# ---------------------------------------------------------------------------
# 2. Rookie path — ALLOWS missing season stats
# ---------------------------------------------------------------------------

class TestRookiePath:
    def test_rookie_with_no_stats_returns_a_bucket(self):
        """True debutant — zero current and prior season — must NOT raise."""
        bucket = predict_rookie_popularity_bucket(
            player_name="Debut Kid", team="KC", is_pitcher=False,
            batting_order=8, as_of=_TODAY,
        )
        assert bucket in {"top_decile", "upper_mid", "mid", "lower_mid", "bottom_decile"}

    def test_rookie_unknown_team_still_raises(self):
        """The team-tier check is enforced regardless of track — unknown
        team is always a data-collection bug, never a rookie carve-out."""
        with pytest.raises(RuntimeError, match="not in TEAM_MARKET_TIER"):
            predict_rookie_popularity_bucket(
                player_name="Debut Kid", team="XYZ", is_pitcher=False,
                batting_order=3, as_of=_TODAY,
            )

    def test_rookie_on_tier1_market_avoids_bottom_decile(self):
        """A Yankees debutant gets at least mid because of market alone."""
        bucket = predict_rookie_popularity_bucket(
            player_name="Yankee Rookie", team="NYY", is_pitcher=False,
            batting_order=2, as_of=_TODAY,
        )
        # NYY tier 1 = +3, top-3 batting = +1 → 4.0 → mid
        assert bucket == "mid"

    def test_flagged_star_rookie_climbs(self):
        """A pre-flagged star prospect (Holliday, Chourio etc.) lifts further."""
        bucket = predict_rookie_popularity_bucket(
            player_name="Jackson Holliday", team="BAL", is_pitcher=False,
            batting_order=1, as_of=_TODAY,
        )
        # BAL tier 3 = +1, star = +3, top-3 = +1 → 5.0 → mid
        assert bucket == "mid"


# ---------------------------------------------------------------------------
# 3. Bucket cutoffs
# ---------------------------------------------------------------------------

class TestBucketMapping:
    @pytest.mark.parametrize("score,expected", [
        (10.0, "top_decile"),
        (8.0,  "top_decile"),
        (7.5,  "upper_mid"),
        (6.0,  "upper_mid"),
        (5.0,  "mid"),
        (3.0,  "mid"),
        (2.0,  "lower_mid"),
        (1.5,  "lower_mid"),
        (1.0,  "bottom_decile"),
        (0.0,  "bottom_decile"),
    ])
    def test_score_to_bucket_boundaries(self, score, expected):
        assert _bucket_from_score(score) == expected


# ---------------------------------------------------------------------------
# 4. Traditional path — produces sensible buckets on real-shape inputs
# ---------------------------------------------------------------------------

class TestTraditionalPath:
    def test_judge_lands_top_decile(self):
        bucket = predict_popularity_bucket(
            player_name="Aaron Judge", team="NYY", is_pitcher=False,
            batting_order=2, season_ops=0.950, season_era=None,
            as_of=_TODAY,
        )
        # NYY tier 1 = +3, star = +3, top-3 = +1, plus fame index → top_decile
        assert bucket == "top_decile"

    def test_anonymous_small_market_lands_bottom_decile(self):
        bucket = predict_popularity_bucket(
            player_name="Joe Schmoe", team="KC", is_pitcher=False,
            batting_order=9, season_ops=0.650, season_era=None,
            as_of=_TODAY,
        )
        # KC tier 4 = 0, no star, no fame, no top-3 → 0.0 → bottom_decile
        assert bucket == "bottom_decile"

    def test_breakout_lifts_via_elite_ops(self):
        """A non-flagged player with OPS >= 0.900 still earns the +2
        elite-stats bonus; catches breakouts before the offseason
        STAR_PLAYER_FLAGS update."""
        cold = predict_popularity_bucket(
            player_name="Anonymous Hitter", team="MIA", is_pitcher=False,
            batting_order=3, season_ops=0.650, season_era=None,
            as_of=_TODAY,
        )
        hot = predict_popularity_bucket(
            player_name="Anonymous Hitter", team="MIA", is_pitcher=False,
            batting_order=3, season_ops=0.950, season_era=None,
            as_of=_TODAY,
        )
        # Hot version should land at least one bucket higher
        order = ["bottom_decile", "lower_mid", "mid", "upper_mid", "top_decile"]
        assert order.index(hot) > order.index(cold)
