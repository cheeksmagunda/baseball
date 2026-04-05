from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload, joinedload

from app.database import get_db
from app.core.utils import find_player_by_name, compute_total_value, get_latest_player_score
from app.models.player import Player
from app.models.slate import Slate, SlatePlayer
from app.models.scoring import ScoreBreakdown, PlayerScore
from app.schemas.scoring import PlayerScoreOut, SlateRankingsOut, TraitBreakdown
from app.services.scoring_engine import score_player
from app.services.pipeline import run_score_slate

router = APIRouter()


@router.post("/player", response_model=PlayerScoreOut)
def score_single_player(
    player_name: str,
    team: str | None = None,
    card_boost: float = 0.0,
    db: Session = Depends(get_db),
):
    """Score a single player on demand."""
    player = find_player_by_name(db, player_name, team)

    if not player:
        raise HTTPException(404, f"Player not found: {player_name}")

    result = score_player(db, player)
    ev = compute_total_value(result.total_score, card_boost) if card_boost else None

    return PlayerScoreOut(
        player_name=result.player_name,
        team=result.team,
        position=result.position,
        total_score=result.total_score,
        card_boost=card_boost,
        expected_value=round(ev, 2) if ev else None,
        breakdowns=[
            TraitBreakdown(
                trait_name=t.name,
                score=t.score,
                max_score=t.max_score,
                raw_value=t.raw_value,
            )
            for t in result.traits
        ],
    )


@router.post("/slate/{slate_date}", response_model=SlateRankingsOut)
def score_slate(slate_date: date, db: Session = Depends(get_db)):
    """Score all players for a slate. Stores results and returns rankings."""
    results = run_score_slate(db, slate_date)
    if not results:
        raise HTTPException(404, "No slate or players found for this date")

    # Look up boosts from slate_players
    slate = (
        db.query(Slate)
        .options(selectinload(Slate.players).joinedload(SlatePlayer.player))
        .filter_by(date=slate_date)
        .first()
    )
    boost_map = {}
    if slate:
        for sp in slate.players:
            if sp.player:
                boost_map[sp.player.name] = sp.card_boost

    rankings = []
    for r in results:
        boost = boost_map.get(r.player_name, 0.0)
        ev = compute_total_value(r.total_score, boost)
        rankings.append(PlayerScoreOut(
            player_name=r.player_name,
            team=r.team,
            position=r.position,
            total_score=r.total_score,
            card_boost=boost,
            expected_value=round(ev, 2),
            breakdowns=[
                TraitBreakdown(
                    trait_name=t.name,
                    score=t.score,
                    max_score=t.max_score,
                    raw_value=t.raw_value,
                )
                for t in r.traits
            ],
        ))

    return SlateRankingsOut(
        date=slate_date.isoformat(),
        player_count=len(rankings),
        rankings=rankings,
    )


@router.get("/{slate_date}/rankings", response_model=SlateRankingsOut)
def get_cached_rankings(slate_date: date, db: Session = Depends(get_db)):
    """Get previously computed rankings for a slate."""
    slate = (
        db.query(Slate)
        .options(
            selectinload(Slate.players)
            .joinedload(SlatePlayer.player),
            selectinload(Slate.players)
            .selectinload(SlatePlayer.scores)
            .selectinload(PlayerScore.breakdowns),
        )
        .filter_by(date=slate_date)
        .first()
    )
    if not slate:
        raise HTTPException(404, "Slate not found")

    rankings = []
    for sp in slate.players:
        player = sp.player
        if not player:
            continue

        # Get latest score from eagerly loaded scores
        ps = max(sp.scores, key=lambda s: s.created_at) if sp.scores else None
        if not ps:
            continue

        breakdowns = ps.breakdowns
        ev = compute_total_value(ps.total_score, sp.card_boost)

        rankings.append(PlayerScoreOut(
            player_name=player.name,
            team=player.team,
            position=player.position,
            total_score=ps.total_score,
            card_boost=sp.card_boost,
            expected_value=round(ev, 2),
            breakdowns=[
                TraitBreakdown(
                    trait_name=b.trait_name,
                    score=b.trait_score,
                    max_score=b.trait_max,
                    raw_value=b.raw_value,
                )
                for b in breakdowns
            ],
        ))

    rankings.sort(key=lambda r: r.total_score, reverse=True)
    return SlateRankingsOut(
        date=slate_date.isoformat(),
        player_count=len(rankings),
        rankings=rankings,
    )
