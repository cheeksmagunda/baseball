from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.player import Player, PlayerStats, PlayerGameLog, normalize_name
from app.schemas.player import PlayerOut, PlayerDetailOut, PlayerStatsOut, PlayerGameLogOut

router = APIRouter()


@router.get("", response_model=list[PlayerOut])
def list_players(
    team: str | None = None,
    position: str | None = None,
    search: str | None = None,
    limit: int = Query(default=50, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    q = db.query(Player)
    if team:
        q = q.filter(Player.team == team.upper())
    if position:
        q = q.filter(Player.position == position.upper())
    if search:
        norm = normalize_name(search)
        q = q.filter(Player.name_normalized.contains(norm))
    return q.offset(offset).limit(limit).all()


@router.get("/{player_id}", response_model=PlayerDetailOut)
def get_player(player_id: int, db: Session = Depends(get_db)):
    player = db.query(Player).get(player_id)
    if not player:
        raise HTTPException(404, "Player not found")

    stats = (
        db.query(PlayerStats)
        .filter_by(player_id=player.id)
        .order_by(PlayerStats.season.desc())
        .all()
    )
    games = (
        db.query(PlayerGameLog)
        .filter_by(player_id=player.id)
        .order_by(PlayerGameLog.game_date.desc())
        .limit(10)
        .all()
    )

    return PlayerDetailOut(
        id=player.id,
        name=player.name,
        team=player.team,
        position=player.position,
        mlb_id=player.mlb_id,
        stats=[PlayerStatsOut.model_validate(s) for s in stats],
        recent_games=[PlayerGameLogOut.model_validate(g) for g in games],
    )
