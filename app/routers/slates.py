from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload, joinedload

from app.database import get_db
from app.core.utils import compute_total_value, find_player_by_name
from app.models.player import Player, normalize_name
from app.models.slate import Slate, SlateGame, SlatePlayer
from app.schemas.slate import SlateOut, SlatePlayerIn, SlatePlayerOut, SlateResultsIn

router = APIRouter()


@router.get("", response_model=list[SlateOut])
def list_slates(db: Session = Depends(get_db)):
    slates = db.query(Slate).order_by(Slate.date.desc()).all()
    return [
        SlateOut(
            id=s.id,
            date=s.date,
            game_count=s.game_count,
            status=s.status,
            games=s.games,
            player_count=len(s.players),
        )
        for s in slates
    ]


@router.get("/{slate_date}", response_model=SlateOut)
def get_slate(slate_date: date, db: Session = Depends(get_db)):
    slate = db.query(Slate).filter_by(date=slate_date).first()
    if not slate:
        raise HTTPException(404, "Slate not found")
    return SlateOut(
        id=slate.id,
        date=slate.date,
        game_count=slate.game_count,
        status=slate.status,
        games=slate.games,
        player_count=len(slate.players),
    )


@router.get("/{slate_date}/players", response_model=list[SlatePlayerOut])
def get_slate_players(slate_date: date, db: Session = Depends(get_db)):
    slate = (
        db.query(Slate)
        .options(
            selectinload(Slate.players).joinedload(SlatePlayer.player),
            selectinload(Slate.games),
        )
        .filter_by(date=slate_date)
        .first()
    )
    if not slate:
        raise HTTPException(404, "Slate not found")

    # Build game lookup for opponent resolution
    game_by_id: dict[int, SlateGame] = {g.id: g for g in slate.games}
    team_to_game: dict[str, SlateGame] = {}
    for g in slate.games:
        team_to_game[g.home_team.upper()] = g
        team_to_game[g.away_team.upper()] = g

    results = []
    for sp in slate.players:
        player = sp.player
        team = player.team if player else ""

        # Resolve opponent team from game context
        opponent_team = None
        game = game_by_id.get(sp.game_id) if sp.game_id else team_to_game.get(team.upper())
        if game:
            opponent_team = game.away_team if game.home_team.upper() == team.upper() else game.home_team

        results.append(SlatePlayerOut(
            id=sp.id,
            player_name=player.name if player else "Unknown",
            team=team,
            position=player.position if player else "",
            card_boost=sp.card_boost,
            real_score=sp.real_score,
            total_value=sp.total_value,
            is_highest_value=sp.is_highest_value,
            drafts=sp.drafts,
            opponent_team=opponent_team,
            batting_order=sp.batting_order,
            platoon_advantage=sp.platoon_advantage,
            is_debut_or_return=sp.is_debut_or_return,
            player_status=sp.player_status,
            env_score=sp.env_score,
        ))
    return results


@router.post("/{slate_date}/players", response_model=list[SlatePlayerOut])
def add_slate_players(
    slate_date: date,
    cards: list[SlatePlayerIn],
    db: Session = Depends(get_db),
):
    """Add available draft cards for a slate."""
    slate = db.query(Slate).filter_by(date=slate_date).first()
    if not slate:
        slate = Slate(date=slate_date, status="pending")
        db.add(slate)
        db.flush()

    results = []
    for card in cards:
        player = find_player_by_name(db, card.player_name, card.team)
        if not player:
            player = Player(
                name=card.player_name,
                name_normalized=normalize_name(card.player_name),
                team=card.team or "UNK",
                position=card.position or "DH",
            )
            db.add(player)
            db.flush()

        sp = SlatePlayer(
            slate_id=slate.id,
            player_id=player.id,
            card_boost=card.card_boost,
            batting_order=card.batting_order,
            platoon_advantage=card.platoon_advantage,
            is_debut_or_return=card.is_debut_or_return,
            drafts=card.drafts,
        )
        db.add(sp)
        db.flush()

        results.append(SlatePlayerOut(
            id=sp.id,
            player_name=player.name,
            team=player.team,
            position=player.position,
            card_boost=sp.card_boost,
            real_score=None,
            total_value=None,
            is_highest_value=False,
            drafts=None,
        ))

    db.commit()
    return results


@router.put("/{slate_date}/results")
def update_slate_results(
    slate_date: date,
    body: SlateResultsIn,
    db: Session = Depends(get_db),
):
    """Post-game: upload actual RS values for a completed slate."""
    slate = db.query(Slate).filter_by(date=slate_date).first()
    if not slate:
        raise HTTPException(404, "Slate not found")

    updated = 0
    for result in body.results:
        player = find_player_by_name(db, result.player_name)
        if not player:
            continue

        sp = (
            db.query(SlatePlayer)
            .filter_by(slate_id=slate.id, player_id=player.id)
            .first()
        )
        if sp:
            sp.real_score = result.real_score
            sp.total_value = compute_total_value(result.real_score, sp.card_boost)
            updated += 1

    slate.status = "completed"
    db.commit()
    return {"updated": updated}
