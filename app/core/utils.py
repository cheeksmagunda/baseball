"""Shared utility functions — single source of truth for common operations."""

from datetime import datetime, timedelta, timezone
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

def get_trait_score(traits: list, trait_name: str) -> float:
    """Extract a specific trait score by name from a list of trait results.

    Works with any object that has .name and .score attributes (e.g. TraitScore).
    Returns 0.0 if the trait is not found.
    """
    for t in traits:
        if t.name == trait_name:
            return t.score
    return 0.0


def scale_score(value: float, floor: float, ceiling: float, max_pts: float) -> float:
    """Linearly scale a value between floor and ceiling to 0..max_pts."""
    if ceiling == floor:
        return max_pts if value >= ceiling else 0.0
    pct = (value - floor) / (ceiling - floor)
    return round(max(0.0, min(max_pts, pct * max_pts)), 1)


def graduated_scale(value: float, floor: float, ceiling: float) -> float:
    """Linearly scale *value* between *floor* and *ceiling* to 0.0–1.0.

    Works for ascending (floor < ceiling) and descending (floor > ceiling) ranges.
    Raises ValueError if *value* is None.
    """
    if value is None:
        raise ValueError("graduated_scale: value must not be None")
    span = ceiling - floor
    if span == 0:
        return 1.0 if value == ceiling else 0.0
    ratio = (value - floor) / span
    return max(0.0, min(1.0, ratio))


def graduated_scale_moneyline(moneyline: int, ml_floor: int, ml_ceiling: int) -> float:
    """Graduated moneyline scale: ml_floor (e.g. -110) → 0, ml_ceiling (e.g. -250) → 1.0.

    More negative = stronger favourite. Raises ValueError if *moneyline* is None.
    """
    if moneyline is None:
        raise ValueError("graduated_scale_moneyline: moneyline must not be None")
    if moneyline <= ml_ceiling:
        return 1.0
    if moneyline >= ml_floor:
        return 0.0
    return (-moneyline - (-ml_floor)) / float(-ml_ceiling - (-ml_floor))


# ---------------------------------------------------------------------------
# Timing validation (T-65 architecture)
# ---------------------------------------------------------------------------

def is_pipeline_callable_now(db: Session) -> tuple[bool, str]:
    """
    Check if the pipeline can be called right now.

    The pipeline must NOT be called during an active slate (games in progress
    or upcoming). It ONLY runs at T-65 via the slate_monitor.

    Returns (True, "") if OK to call, (False, reason) if NOT.
    """
    from app.models.slate import Slate, SlateGame
    from datetime import date

    today = date.today()

    # Check if today's slate has active games
    today_slate = db.query(Slate).filter_by(date=today).first()
    if today_slate:
        games = db.query(SlateGame).filter_by(slate_id=today_slate.id).all()
        if games:
            # Slate exists with games — check if any are still in progress
            all_final = all(
                (g.home_score is not None and g.away_score is not None)
                or g.game_status in ("Postponed", "Cancelled", "Suspended")
                for g in games
            )
            if not all_final:
                # Games still playing — pipeline must NOT be called
                return (
                    False,
                    "Pipeline is locked during active slate. "
                    "Picks are generated at T-65 by the slate monitor and cached. "
                    "Manual calls are only allowed after all games finish."
                )

    # No active slate today — OK to call
    return (True, "")
