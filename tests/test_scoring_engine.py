"""Tests for the scoring engine using known HV player data."""

import pytest

from app.models.player import Player, PlayerStats, PlayerGameLog
from app.services.scoring_engine import (
    score_pitcher,
    score_batter,
    score_ace_status,
    score_pitcher_k_rate,
    score_offensive_profile,
    score_hot_streak,
)


def test_ace_status_great_era():
    stats = PlayerStats(id=1, player_id=1, season=2026, era=2.3, ip=100, k_per_9=10.0)
    result = score_ace_status(stats, 25.0)
    assert result.score == 25.0


def test_ace_status_bad_era():
    stats = PlayerStats(id=1, player_id=1, season=2026, era=5.5, ip=50, k_per_9=6.0)
    result = score_ace_status(stats, 25.0)
    assert result.score <= 5.0


def test_k_rate_elite():
    stats = PlayerStats(id=1, player_id=1, season=2026, k_per_9=12.0, ip=100)
    result = score_pitcher_k_rate(stats, 25.0, team_framing_runs=0.0)
    assert result.score == 25.0


def test_k_rate_below_floor():
    stats = PlayerStats(id=1, player_id=1, season=2026, k_per_9=5.0, ip=100)
    result = score_pitcher_k_rate(stats, 25.0, team_framing_runs=0.0)
    assert result.score == 0


def test_offensive_profile_slugger():
    """Judge-tier slugger: high OPS + saturated Statcast signals → ~max."""
    stats = PlayerStats(
        id=1, player_id=1, season=2026,
        pa=200, hr=15, ops=0.985, x_woba=0.420,
        avg_exit_velocity=94.0, hard_hit_pct=58.0, barrel_pct=18.0,
    )
    result = score_offensive_profile(stats, 40.0)
    assert result.score >= 36.0
    assert result.name == "offensive_profile"


def test_offensive_profile_high_ops_contact_hitter():
    """Arraez-tier contact hitter: high OPS, weak Statcast power.

    Pre-V13.1 (kinematics-only): ~6/40.  V13.1 OPS-anchored: 12-16/40.
    The contact hitter must be visibly competitive once env conditions favor
    them (V12 EV multiplier × good env_factor closes the rest of the gap).
    """
    stats = PlayerStats(
        id=1, player_id=1, season=2026,
        pa=200, hr=2, ops=0.830, x_woba=0.330,
        avg_exit_velocity=87.0, hard_hit_pct=32.0, barrel_pct=5.0,
    )
    result = score_offensive_profile(stats, 40.0)
    assert 11.0 <= result.score <= 17.0


def test_offensive_profile_low_ops():
    """Below-floor OPS (<0.700) + weak Statcast = bottom of pool."""
    stats = PlayerStats(
        id=1, player_id=1, season=2026,
        pa=200, hr=1, ops=0.620, x_woba=0.290,
        avg_exit_velocity=84.0, hard_hit_pct=28.0, barrel_pct=2.0,
    )
    result = score_offensive_profile(stats, 40.0)
    assert result.score <= 5.0


def test_offensive_profile_strict_raises_on_missing_ops():
    """Strict semantics: OPS is required, even if Statcast is fully populated."""
    stats = PlayerStats(
        id=1, player_id=1, season=2026,
        pa=200, hr=15, ops=None, x_woba=0.420,
        avg_exit_velocity=94.0, hard_hit_pct=58.0, barrel_pct=18.0,
    )
    with pytest.raises(RuntimeError, match="OPS is None"):
        score_offensive_profile(stats, 40.0)


def test_offensive_profile_ops_only_when_no_statcast():
    """Call-up with OPS but no Savant row yet — scores from OPS alone."""
    stats = PlayerStats(
        id=1, player_id=1, season=2026,
        pa=60, ops=0.875, x_woba=None,
        avg_exit_velocity=None, hard_hit_pct=None, barrel_pct=None,
    )
    result = score_offensive_profile(stats, 40.0)
    # OPS=0.875 → (0.875-0.700)/(0.950-0.700) = 0.70 → 0.70 × 40 = 28.0
    assert 27.0 <= result.score <= 29.0


def test_hot_streak_three_multi_hit():
    from datetime import date
    logs = [
        PlayerGameLog(id=i, player_id=1, game_date=date(2026, 3, 28 + i), hits=2+i)
        for i in range(3)
    ]
    result = score_hot_streak(logs, 10.0)
    assert result.score == 10.0


def test_hot_streak_cold():
    from datetime import date
    logs = [
        PlayerGameLog(id=i, player_id=1, game_date=date(2026, 3, 28 + i), hits=0)
        for i in range(3)
    ]
    result = score_hot_streak(logs, 10.0)
    assert result.score == 0


def test_pitcher_full_score():
    """Test scoring a full pitcher profile (Cristopher Sanchez-like)."""
    player = Player(id=1, name="Test Ace", name_normalized="test ace", team="PHI", position="P")
    stats = PlayerStats(
        id=1, player_id=1, season=2026,
        era=2.2, whip=0.95, k_per_9=11.0, ip=120, games=20,
    )
    from datetime import date
    logs = [
        PlayerGameLog(
            id=i, player_id=1, game_date=date(2026, 3, 25 + i),
            ip=6.0, er=0, k_pitching=8,
        )
        for i in range(3)
    ]

    result = score_pitcher(player, stats, logs, team_framing_runs=0.0)
    assert result.total_score >= 75


def test_batter_full_score():
    """Test scoring a full batter profile (power hitter)."""
    player = Player(id=2, name="Test Slugger", name_normalized="test slugger", team="COL", position="OF")
    stats = PlayerStats(
        id=2, player_id=2, season=2026,
        pa=250, hr=18, sb=5, iso=0.260, barrel_pct=13.0,
        avg=0.285, ops=0.900, ab=220, hits=63, games=60,
    )
    from datetime import date
    logs = [
        PlayerGameLog(
            id=i, player_id=2, game_date=date(2026, 3, 27 + i),
            ab=4, hits=2, hr=1, rbi=2,
        )
        for i in range(5)
    ]

    result = score_batter(player, stats, logs)
    assert result.total_score >= 65
