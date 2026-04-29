"""Tests for the scoring engine using known HV player data."""

from app.models.player import Player, PlayerStats, PlayerGameLog
from app.services.scoring_engine import (
    score_pitcher,
    score_pitcher_matchup,
    score_batter,
    score_ace_status,
    score_pitcher_k_rate,
    score_power_profile,
    score_lineup_position,
    score_ballpark_factor,
    score_hot_streak,
    score_batter_matchup,
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


def test_pitcher_matchup_populated_returns_vs_label():
    """Regression: when opp_team and opp_team_stats are both provided, the
    matchup scorer must compute a real score with a 'vs ...' raw_value and
    NOT the 'matchup unknown' neutral fallback.

    This is the contract the candidate_resolver and run_score_slate call
    sites rely on.
    """
    max_pts = 20.0
    result = score_pitcher_matchup(
        opp_team="BAL",
        opp_stats={"ops": 0.700, "k_pct": 0.24},
        max_pts=max_pts,
    )
    assert result.raw_value is not None
    assert result.raw_value.startswith("vs ")
    assert "matchup unknown" not in result.raw_value
    assert result.score != max_pts * 0.5


def test_pitcher_matchup_missing_opp_team_returns_unknown():
    """The guard in score_pitcher_matchup requires BOTH opp_team and
    opp_stats. Document and lock in the contract: omitting opp_team still
    returns the neutral fallback, even when stats are present."""
    max_pts = 20.0
    result = score_pitcher_matchup(
        opp_team=None,
        opp_stats={"ops": 0.700, "k_pct": 0.24},
        max_pts=max_pts,
    )
    assert result.raw_value == "matchup unknown"
    assert result.score == max_pts * 0.5


def test_score_pitcher_forwards_opp_team_to_matchup():
    """Integration: score_pitcher must forward opp_team + opp_team_stats to
    the matchup trait. If it drops either, matchup falls back to neutral."""
    player = Player(id=1, name="Test SP", name_normalized="test sp", team="SEA", position="P")
    stats = PlayerStats(id=1, player_id=1, season=2026, era=3.5, whip=1.15, k_per_9=9.0, ip=50)
    logs: list[PlayerGameLog] = []

    result = score_pitcher(
        player, stats, logs,
        opp_team="OAK",
        opp_team_stats={"ops": 0.680, "k_pct": 0.25},
    )
    matchup = next(t for t in result.traits if t.name == "matchup_quality")
    assert matchup.raw_value is not None
    assert matchup.raw_value.startswith("vs ")


# ---------------------------------------------------------------------------
# V10.6 batter K-vulnerability sub-signal
#
# Cross-axis penalty: high-K batter × elite K-pitcher = floor risk (0-fer).
# Both have to be high; either side alone passes through.
# ---------------------------------------------------------------------------


def _high_k_bat() -> PlayerStats:
    """Joey Gallo / Schwarber-class profile — 33% K rate."""
    return PlayerStats(id=10, player_id=10, season=2026, pa=300, ab=260, so=100)


def _contact_bat() -> PlayerStats:
    """Arraez / Tucker-class — 12% K rate, well below the floor."""
    return PlayerStats(id=11, player_id=11, season=2026, pa=300, ab=270, so=36)


def test_k_vuln_fires_only_on_high_k_bat_vs_high_k_pitcher():
    """The full penalty fires only when BOTH batter K% and opp K/9 are high.
    Otherwise the cross product is small and the matchup score is preserved."""
    high_k_pitcher = {"era": 3.0, "whip": 1.10, "k_per_9": 11.5}
    contact_pitcher = {"era": 3.0, "whip": 1.10, "k_per_9": 6.5}

    # Hi-K bat vs hi-K pitcher → cross fires → matchup score ↓
    danger = score_batter_matchup(high_k_pitcher, "L", 20.0, batter_stats=_high_k_bat())
    # Hi-K bat vs contact pitcher → batter K-vuln muted (no cross K-arm)
    safe_pitcher = score_batter_matchup(contact_pitcher, "L", 20.0, batter_stats=_high_k_bat())
    # Contact bat vs hi-K pitcher → batter floor protects, no cross
    safe_bat = score_batter_matchup(high_k_pitcher, "L", 20.0, batter_stats=_contact_bat())

    assert danger.score < safe_pitcher.score, (
        "High-K bat vs elite K-arm must score worse than the same bat vs a contact pitcher"
    )
    assert danger.score < safe_bat.score, (
        "High-K bat vs elite K-arm must score worse than a contact bat vs the same pitcher"
    )


def test_k_vuln_skipped_when_batter_has_no_pa():
    """Rookie batters with PA=0 must not divide-by-zero or fire a phantom penalty."""
    rookie = PlayerStats(id=12, player_id=12, season=2026, pa=0, so=0)
    pitcher = {"era": 3.0, "whip": 1.10, "k_per_9": 11.5}
    result = score_batter_matchup(pitcher, "L", 20.0, batter_stats=rookie)
    # Should fall back to the era+whip-only blend; no K-vuln in the detail string.
    assert "K-vuln" not in result.raw_value


def test_k_vuln_skipped_when_opp_k9_unknown():
    """If we don't have opp K/9, K-vuln cannot evaluate and must drop out cleanly."""
    bat = _high_k_bat()
    pitcher = {"era": 3.0, "whip": 1.10}  # no k_per_9
    result = score_batter_matchup(pitcher, "L", 20.0, batter_stats=bat)
    assert "K-vuln" not in result.raw_value
    assert result.score > 0


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
