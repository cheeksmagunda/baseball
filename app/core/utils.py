"""Shared utility functions — single source of truth for common operations."""

from sqlalchemy.orm import Session

from app.models.player import Player, PlayerGameLog, normalize_name
from app.models.scoring import PlayerScore


# ---------------------------------------------------------------------------
# Total Value calculation — THE core formula for Real Sports DFS
# total_value = real_score × (2 + card_boost)
# This is NOT traditional DFS. The base multiplier is always 2.
# ---------------------------------------------------------------------------

BASE_MULTIPLIER = 2.0


def compute_total_value(real_score: float, card_boost: float) -> float:
    """Compute total_value = real_score * (2 + card_boost)."""
    return real_score * (BASE_MULTIPLIER + card_boost)


def compute_expected_total_value(estimated_rs: float, card_boost: float) -> float:
    """Compute expected total value from estimated RS and card boost."""
    return round(estimated_rs * (BASE_MULTIPLIER + card_boost), 2)


# ---------------------------------------------------------------------------
# Player lookup
# ---------------------------------------------------------------------------

def find_player_by_name(
    db: Session, name: str, team: str | None = None
) -> Player | None:
    """Find a player by name (accent-insensitive). Optionally filter by team."""
    norm = normalize_name(name)
    q = db.query(Player).filter(Player.name_normalized.contains(norm))
    if team:
        q = q.filter(Player.team == team.upper())
    return q.first()


# ---------------------------------------------------------------------------
# Score queries
# ---------------------------------------------------------------------------

def get_latest_player_score(db: Session, slate_player_id: int) -> PlayerScore | None:
    """Get the most recent PlayerScore for a slate player."""
    return (
        db.query(PlayerScore)
        .filter_by(slate_player_id=slate_player_id)
        .order_by(PlayerScore.created_at.desc())
        .first()
    )


# ---------------------------------------------------------------------------
# Game log helpers
# ---------------------------------------------------------------------------

def get_recent_games(
    game_logs: list[PlayerGameLog], n: int
) -> list[PlayerGameLog]:
    """Return the N most recent games, sorted by date descending."""
    return sorted(game_logs, key=lambda g: g.game_date, reverse=True)[:n]


# ---------------------------------------------------------------------------
# Scoring math helpers
# ---------------------------------------------------------------------------

def scale_score(value: float, floor: float, ceiling: float, max_pts: float) -> float:
    """Linearly scale a value between floor and ceiling to 0..max_pts."""
    if ceiling == floor:
        return max_pts if value >= ceiling else 0.0
    pct = (value - floor) / (ceiling - floor)
    return round(max(0.0, min(max_pts, pct * max_pts)), 1)
