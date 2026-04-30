from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.core.utils import find_player_by_name
from app.models.slate import Slate, SlatePlayer
from app.models.scoring import PlayerScore
from app.schemas.scoring import PlayerScoreOut, SlateRankingsOut, TraitBreakdown
from app.services.scoring_engine import score_player
from app.services.pipeline import run_score_slate

router = APIRouter()


@router.post("/player", response_model=PlayerScoreOut)
def score_single_player(
    player_name: str,
    team: str | None = None,
    db: Session = Depends(get_db),
):
    """Score a single player on demand.  Returns the intrinsic 0-100 trait score."""
    player = find_player_by_name(db, player_name, team)

    if not player:
        raise HTTPException(404, f"Player not found: {player_name}")

    result = score_player(db, player)

    return PlayerScoreOut(
        player_name=result.player_name,
        team=result.team,
        position=result.position,
        total_score=result.total_score,
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
    """Score all players for a slate.  Stores results and returns intrinsic rankings."""
    results = run_score_slate(db, slate_date)
    if not results:
        raise HTTPException(404, "No slate or players found for this date")

    rankings = [
        PlayerScoreOut(
            player_name=r.player_name,
            team=r.team,
            position=r.position,
            total_score=r.total_score,
            breakdowns=[
                TraitBreakdown(
                    trait_name=t.name,
                    score=t.score,
                    max_score=t.max_score,
                    raw_value=t.raw_value,
                )
                for t in r.traits
            ],
        )
        for r in results
    ]

    return SlateRankingsOut(
        date=slate_date.isoformat(),
        player_count=len(rankings),
        rankings=rankings,
    )


@router.get("/{slate_date}/rankings", response_model=SlateRankingsOut)
def get_cached_rankings(slate_date: date, db: Session = Depends(get_db)):
    """Get previously computed rankings for a slate.  Intrinsic scores only."""
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

        rankings.append(PlayerScoreOut(
            player_name=player.name,
            team=player.team,
            position=player.position,
            total_score=ps.total_score,
            breakdowns=[
                TraitBreakdown(
                    trait_name=b.trait_name,
                    score=b.trait_score,
                    max_score=b.trait_max,
                    raw_value=b.raw_value,
                )
                for b in ps.breakdowns
            ],
        ))

    rankings.sort(key=lambda r: r.total_score, reverse=True)
    return SlateRankingsOut(
        date=slate_date.isoformat(),
        player_count=len(rankings),
        rankings=rankings,
    )
