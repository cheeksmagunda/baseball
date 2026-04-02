"""Tests for the scoring engine using known HV player data."""

from app.models.player import Player, PlayerStats, PlayerGameLog
from app.services.scoring_engine import (
    score_pitcher,
    score_batter,
    score_ace_status,
    score_pitcher_k_rate,
    score_power_profile,
    score_lineup_position,
    score_ballpark_factor,
    score_hot_streak,
)
from app.core.weights import ScoringWeights


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
    result = score_pitcher_k_rate(stats, 25.0)
    assert result.score == 25.0


def test_k_rate_below_floor():
    stats = PlayerStats(id=1, player_id=1, season=2026, k_per_9=5.0, ip=100)
    result = score_pitcher_k_rate(stats, 25.0)
    assert result.score == 0


def test_power_profile_slugger():
    stats = PlayerStats(
        id=1, player_id=1, season=2026,
        pa=200, hr=15, iso=0.280, barrel_pct=14.0,
    )
    result = score_power_profile(stats, 25.0)
    assert result.score >= 20.0


def test_power_profile_no_power():
    stats = PlayerStats(
        id=1, player_id=1, season=2026,
        pa=200, hr=1, iso=0.050, barrel_pct=2.0,
    )
    result = score_power_profile(stats, 25.0)
    assert result.score <= 8.0


def test_lineup_position_cleanup():
    result = score_lineup_position(3, 15.0)
    assert result.score == 15.0


def test_lineup_position_bottom():
    result = score_lineup_position(9, 15.0)
    assert result.score <= 5.0


def test_ballpark_coors():
    result = score_ballpark_factor("COL", 10.0)
    assert result.score == 10.0


def test_ballpark_pitcher_park():
    result = score_ballpark_factor("LAD", 10.0)
    assert result.score <= 2.0


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

    result = score_pitcher(player, stats, logs)
    # A dominant ace should score 75+
    assert result.total_score >= 75


def test_batter_full_score():
    """Test scoring a full batter profile (power hitter)."""
    player = Player(id=2, name="Test Slugger", name_normalized="test slugger", team="COL", position="OF")
    stats = PlayerStats(
        id=2, player_id=2, season=2026,
        pa=250, hr=18, sb=5, iso=0.260, barrel_pct=13.0,
        avg=0.285, ops=0.900, ab=220, hits=63,
    )
    from datetime import date
    logs = [
        PlayerGameLog(
            id=i, player_id=2, game_date=date(2026, 3, 27 + i),
            ab=4, hits=2, hr=1, rbi=2,
        )
        for i in range(5)
    ]

    result = score_batter(player, stats, logs, batting_order=3, park_team="COL")
    # Power hitter at Coors batting 3rd should score 70+
    assert result.total_score >= 65
