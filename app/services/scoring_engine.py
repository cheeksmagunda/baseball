"""
Rule-based scoring engine for DFS player evaluation.

Scores players 0-100 based on trait profiles derived from
Highest Value player analysis across March 25-31, 2026 data.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.core.constants import PARK_HR_FACTORS, PITCHER_POSITIONS, SCORE_TO_RS_RANGES
from app.core.utils import get_recent_games, scale_score
from app.core.weights import ScoringWeights, get_current_weights
from app.models.player import Player, PlayerGameLog, PlayerStats


@dataclass
class TraitResult:
    name: str
    score: float
    max_score: float
    raw_value: str = ""


@dataclass
class PlayerScoreResult:
    player_name: str
    team: str
    position: str
    total_score: float
    estimated_rs_low: float
    estimated_rs_high: float
    estimated_rs_mid: float
    traits: list[TraitResult]


# ---------------------------------------------------------------------------
# Score-to-RS mapping
# ---------------------------------------------------------------------------

def score_to_rs_range(score: float) -> tuple[float, float, float]:
    """Map a 0-100 score to (rs_low, rs_high, rs_mid)."""
    for s_low, s_high, rs_low, rs_high in SCORE_TO_RS_RANGES:
        if s_low <= score <= s_high:
            # Interpolate within the range
            pct = (score - s_low) / max(s_high - s_low, 1)
            rs_mid = rs_low + pct * (rs_high - rs_low)
            return rs_low, rs_high, round(rs_mid, 2)
    return -0.5, 0.5, 0.0


# ---------------------------------------------------------------------------
# Pitcher trait scorers
# ---------------------------------------------------------------------------

def score_ace_status(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Score based on pitcher quality indicators (IP, ERA as proxy for rotation rank)."""
    if not stats or stats.ip == 0:
        return TraitResult("ace_status", 0, max_pts, "no stats")

    # Use ERA as proxy: <2.5 = ace, <3.5 = solid, <4.5 = average, >4.5 = back-end
    era = stats.era or 5.0
    if era < 2.5:
        score = max_pts
    elif era < 3.0:
        score = max_pts * 0.85
    elif era < 3.5:
        score = max_pts * 0.7
    elif era < 4.0:
        score = max_pts * 0.5
    elif era < 4.5:
        score = max_pts * 0.3
    else:
        score = max_pts * 0.1

    return TraitResult("ace_status", round(score, 1), max_pts, f"ERA={era:.2f}")


def score_pitcher_k_rate(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Score K/9 rate. Higher = better. Scale: 6 K/9 = 0, 12+ K/9 = max."""
    if not stats or stats.k_per_9 is None:
        return TraitResult("k_rate", 0, max_pts, "no K/9 data")

    k9 = stats.k_per_9
    # Linear scale: 6 = 0%, 12 = 100%
    score = max(0, min(max_pts, (k9 - 6.0) / 6.0 * max_pts))
    return TraitResult("k_rate", round(score, 1), max_pts, f"K/9={k9:.1f}")


def score_pitcher_matchup(
    opp_team: str | None, opp_stats: dict | None, max_pts: float
) -> TraitResult:
    """Score based on opponent offensive quality. Weaker opponent = higher score."""
    if not opp_team or not opp_stats:
        # Default to middle score when matchup unknown
        return TraitResult("matchup_quality", max_pts * 0.5, max_pts, "matchup unknown")

    # Use opponent team OPS; lower is better for pitcher
    opp_ops = opp_stats.get("ops", 0.730)
    opp_k_pct = opp_stats.get("k_pct", 0.22)

    # OPS component: .650 or below = max, .800+ = 0
    ops_score = max(0, min(1, (0.800 - opp_ops) / 0.150))
    # K% component: .28+ = max, .18 or below = 0
    k_score = max(0, min(1, (opp_k_pct - 0.18) / 0.10))

    combined = (ops_score * 0.6 + k_score * 0.4) * max_pts
    return TraitResult(
        "matchup_quality",
        round(combined, 1),
        max_pts,
        f"opp_OPS={opp_ops:.3f} opp_K%={opp_k_pct:.3f}",
    )


def score_pitcher_recent_form(
    game_logs: list[PlayerGameLog], max_pts: float
) -> TraitResult:
    """Score based on last 3 starts. QS-like quality = high score."""
    if not game_logs:
        return TraitResult("recent_form", max_pts * 0.4, max_pts, "no recent games")

    recent = get_recent_games(game_logs, 3)
    total_score = 0
    for g in recent:
        # Quality start proxy: 5+ IP and <=3 ER
        if g.ip >= 5.0 and g.er <= 3:
            if g.er == 0:
                total_score += 1.0  # Shutout start
            elif g.er <= 1:
                total_score += 0.85
            else:
                total_score += 0.6
        elif g.ip >= 4.0 and g.er <= 2:
            total_score += 0.5
        else:
            total_score += 0.15

    avg_score = total_score / len(recent)
    result = avg_score * max_pts
    return TraitResult(
        "recent_form",
        round(result, 1),
        max_pts,
        f"{len(recent)} starts, avg_quality={avg_score:.2f}",
    )


def score_pitcher_era_whip(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Combined ERA + WHIP score."""
    if not stats:
        return TraitResult("era_whip", 0, max_pts, "no stats")

    era = stats.era or 5.0
    whip = stats.whip or 1.5

    # ERA component: <2 = max, >5 = 0
    era_score = max(0, min(1, (5.0 - era) / 3.0))
    # WHIP component: <0.9 = max, >1.5 = 0
    whip_score = max(0, min(1, (1.5 - whip) / 0.6))

    combined = (era_score * 0.6 + whip_score * 0.4) * max_pts
    return TraitResult(
        "era_whip",
        round(combined, 1),
        max_pts,
        f"ERA={era:.2f} WHIP={whip:.2f}",
    )


# ---------------------------------------------------------------------------
# Batter trait scorers
# ---------------------------------------------------------------------------

def score_power_profile(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Score power based on HR rate, barrel%, ISO."""
    if not stats or stats.pa == 0:
        return TraitResult("power_profile", 0, max_pts, "no stats")

    hr_per_pa = stats.hr / max(stats.pa, 1)
    iso = stats.iso or 0.0
    barrel_pct = stats.barrel_pct or 0.0

    # HR/PA: .06+ = max (10pts), scale from 0
    hr_score = min(10, hr_per_pa / 0.06 * 10)
    # Barrel%: 15%+ = max (8pts)
    barrel_score = min(8, barrel_pct / 15.0 * 8)
    # ISO: .250+ = max (7pts)
    iso_score = min(7, iso / 0.250 * 7)

    total = (hr_score + barrel_score + iso_score) / 25.0 * max_pts
    return TraitResult(
        "power_profile",
        round(total, 1),
        max_pts,
        f"HR/PA={hr_per_pa:.3f} barrel={barrel_pct:.1f}% ISO={iso:.3f}",
    )


def score_lineup_position(batting_order: int | None, max_pts: float) -> TraitResult:
    """Score based on where they bat. 2-4 = best RBI spots."""
    if batting_order is None:
        return TraitResult("lineup_position", max_pts * 0.5, max_pts, "lineup unknown")

    if batting_order in (2, 3, 4):
        score = max_pts
    elif batting_order in (1, 5):
        score = max_pts * 0.8
    elif batting_order in (6, 7):
        score = max_pts * 0.5
    else:
        score = max_pts * 0.25

    return TraitResult("lineup_position", round(score, 1), max_pts, f"bats #{batting_order}")


def score_batter_matchup(
    opp_pitcher_stats: dict | None, batter_hand: str | None, max_pts: float
) -> TraitResult:
    """Score matchup vs opposing starter. Higher opponent ERA = better for batter."""
    if not opp_pitcher_stats:
        return TraitResult("matchup_quality", max_pts * 0.5, max_pts, "matchup unknown")

    opp_era = opp_pitcher_stats.get("era", 4.0)
    opp_whip = opp_pitcher_stats.get("whip", 1.3)

    # Opponent ERA: >5 = great for batter (max), <2.5 = terrible (0)
    era_score = max(0, min(1, (opp_era - 2.5) / 2.5))
    # Opponent WHIP: >1.5 = great (max), <0.9 = terrible (0)
    whip_score = max(0, min(1, (opp_whip - 0.9) / 0.6))

    combined = (era_score * 0.6 + whip_score * 0.4) * max_pts
    return TraitResult(
        "matchup_quality",
        round(combined, 1),
        max_pts,
        f"vs_ERA={opp_era:.2f} vs_WHIP={opp_whip:.2f}",
    )


def score_batter_recent_form(
    game_logs: list[PlayerGameLog], max_pts: float
) -> TraitResult:
    """Score last 7 games by OPS-like metric."""
    if not game_logs:
        return TraitResult("recent_form", max_pts * 0.4, max_pts, "no recent games")

    recent = get_recent_games(game_logs, 7)
    total_h = sum(g.hits for g in recent)
    total_ab = sum(g.ab for g in recent) or 1
    total_hr = sum(g.hr for g in recent)
    total_rbi = sum(g.rbi for g in recent)

    avg = total_h / total_ab
    # Weighted: AVG matters, but HR and RBI production boosts it
    production = avg + (total_hr * 0.05) + (total_rbi * 0.02)
    score = min(max_pts, production / 0.5 * max_pts)

    return TraitResult(
        "recent_form",
        round(score, 1),
        max_pts,
        f"last {len(recent)}G: {total_h}/{total_ab} ({avg:.3f}) {total_hr}HR {total_rbi}RBI",
    )


def score_ballpark_factor(park_team: str | None, max_pts: float) -> TraitResult:
    """Score based on home ballpark HR factor."""
    if not park_team:
        return TraitResult("ballpark_factor", max_pts * 0.5, max_pts, "park unknown")

    factor = PARK_HR_FACTORS.get(park_team, 1.0)
    # Scale: 0.89 (LAD) → 0, 1.38 (COL) → max
    score = max(0, min(max_pts, (factor - 0.89) / (1.38 - 0.89) * max_pts))
    return TraitResult(
        "ballpark_factor", round(score, 1), max_pts, f"park={park_team} factor={factor:.2f}"
    )


def score_hot_streak(game_logs: list[PlayerGameLog], max_pts: float) -> TraitResult:
    """Count multi-hit games in last 3 days."""
    if not game_logs:
        return TraitResult("hot_streak", 0, max_pts, "no recent games")

    recent = get_recent_games(game_logs, 3)
    multi_hit = sum(1 for g in recent if g.hits >= 2)

    if multi_hit >= 3:
        score = max_pts
    elif multi_hit == 2:
        score = max_pts * 0.7
    elif multi_hit == 1:
        score = max_pts * 0.4
    else:
        score = 0

    return TraitResult("hot_streak", round(score, 1), max_pts, f"{multi_hit}/3 multi-hit days")


def score_speed_component(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Score stolen base potential."""
    if not stats:
        return TraitResult("speed_component", 0, max_pts, "no stats")

    games = max(stats.games or 0, 1)
    sb_pace = stats.sb / games * 162  # Project to full season

    if sb_pace >= 30:
        score = max_pts
    elif sb_pace >= 20:
        score = max_pts * 0.8
    elif sb_pace >= 10:
        score = max_pts * 0.5
    elif sb_pace >= 5:
        score = max_pts * 0.3
    else:
        score = max_pts * 0.1

    return TraitResult("speed_component", round(score, 1), max_pts, f"SB_pace={sb_pace:.0f}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def score_pitcher(
    player: Player,
    stats: PlayerStats | None,
    game_logs: list[PlayerGameLog],
    opp_team: str | None = None,
    opp_team_stats: dict | None = None,
    weights: ScoringWeights | None = None,
) -> PlayerScoreResult:
    """Score a pitcher on all traits."""
    w = (weights or ScoringWeights()).pitcher

    traits = [
        score_ace_status(stats, w.ace_status),
        score_pitcher_k_rate(stats, w.k_rate),
        score_pitcher_matchup(opp_team, opp_team_stats, w.matchup_quality),
        score_pitcher_recent_form(game_logs, w.recent_form),
        score_pitcher_era_whip(stats, w.era_whip),
    ]

    total = sum(t.score for t in traits)
    rs_low, rs_high, rs_mid = score_to_rs_range(total)

    return PlayerScoreResult(
        player_name=player.name,
        team=player.team,
        position=player.position,
        total_score=round(total, 1),
        estimated_rs_low=rs_low,
        estimated_rs_high=rs_high,
        estimated_rs_mid=rs_mid,
        traits=traits,
    )


def score_batter(
    player: Player,
    stats: PlayerStats | None,
    game_logs: list[PlayerGameLog],
    batting_order: int | None = None,
    opp_pitcher_stats: dict | None = None,
    park_team: str | None = None,
    weights: ScoringWeights | None = None,
) -> PlayerScoreResult:
    """Score a batter on all traits."""
    w = (weights or ScoringWeights()).batter

    traits = [
        score_power_profile(stats, w.power_profile),
        score_lineup_position(batting_order, w.lineup_position),
        score_batter_matchup(opp_pitcher_stats, None, w.matchup_quality),
        score_batter_recent_form(game_logs, w.recent_form),
        score_ballpark_factor(park_team, w.ballpark_factor),
        score_hot_streak(game_logs, w.hot_streak),
        score_speed_component(stats, w.speed_component),
    ]

    total = sum(t.score for t in traits)
    rs_low, rs_high, rs_mid = score_to_rs_range(total)

    return PlayerScoreResult(
        player_name=player.name,
        team=player.team,
        position=player.position,
        total_score=round(total, 1),
        estimated_rs_low=rs_low,
        estimated_rs_high=rs_high,
        estimated_rs_mid=rs_mid,
        traits=traits,
    )


def score_player(
    db: Session,
    player: Player,
    game_date: date | None = None,
    opp_team: str | None = None,
    opp_team_stats: dict | None = None,
    opp_pitcher_stats: dict | None = None,
    batting_order: int | None = None,
    park_team: str | None = None,
) -> PlayerScoreResult:
    """Score any player (auto-detects pitcher vs batter)."""
    from app.config import settings

    weights = get_current_weights(db)
    stats = (
        db.query(PlayerStats)
        .filter_by(player_id=player.id, season=settings.current_season)
        .first()
    )
    game_logs = (
        db.query(PlayerGameLog)
        .filter_by(player_id=player.id)
        .order_by(PlayerGameLog.game_date.desc())
        .limit(10)
        .all()
    )

    is_pitcher = player.position in PITCHER_POSITIONS

    if is_pitcher:
        return score_pitcher(
            player, stats, game_logs,
            opp_team=opp_team,
            opp_team_stats=opp_team_stats,
            weights=weights,
        )
    else:
        return score_batter(
            player, stats, game_logs,
            batting_order=batting_order,
            opp_pitcher_stats=opp_pitcher_stats,
            park_team=park_team or opp_team,
            weights=weights,
        )
