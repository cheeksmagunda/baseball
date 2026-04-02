from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.player import Player
from app.models.slate import Slate, SlatePlayer
from app.models.scoring import PlayerScore
from app.schemas.popularity import (
    PopularityPlayerIn,
    PopularityProfileOut,
    PopularitySignalOut,
    SlatePopularityOut,
)
from app.services.popularity import (
    PopularityClass,
    get_popularity_profile,
    get_slate_popularity,
)

router = APIRouter()


@router.post("/player", response_model=PopularityProfileOut)
async def check_player_popularity(req: PopularityPlayerIn):
    """Check popularity signals for a single player."""
    profile = await get_popularity_profile(
        req.player_name, req.team, req.player_score
    )
    return PopularityProfileOut(
        player_name=profile.player_name,
        team=profile.team,
        social_score=profile.social_score,
        news_score=profile.news_score,
        dfs_ownership_score=profile.dfs_ownership_score,
        search_score=profile.search_score,
        composite_score=profile.composite_score,
        classification=profile.classification.value,
        reason=profile.reason,
        signals=[
            PopularitySignalOut(source=s.source, score=s.score, context=s.context)
            for s in profile.signals
        ],
    )


@router.post("/slate/{slate_date}", response_model=SlatePopularityOut)
async def check_slate_popularity(slate_date: date, db: Session = Depends(get_db)):
    """
    Run popularity analysis for all players in a slate.

    Returns each player classified as FADE, TARGET, or NEUTRAL,
    sorted by composite popularity score (most popular first).
    """
    slate = db.query(Slate).filter_by(date=slate_date).first()
    if not slate:
        raise HTTPException(404, "Slate not found")

    # Build player list with their performance scores
    players_input = []
    for sp in slate.players:
        player = db.query(Player).get(sp.player_id)
        if not player:
            continue

        # Get performance score if available
        ps = (
            db.query(PlayerScore)
            .filter_by(slate_player_id=sp.id)
            .order_by(PlayerScore.created_at.desc())
            .first()
        )
        player_score = ps.total_score if ps else 50.0

        players_input.append({
            "player_name": player.name,
            "team": player.team,
            "player_score": player_score,
        })

    if not players_input:
        raise HTTPException(404, "No players found for this slate")

    profiles = await get_slate_popularity(players_input)

    out_profiles = [
        PopularityProfileOut(
            player_name=p.player_name,
            team=p.team,
            social_score=p.social_score,
            news_score=p.news_score,
            dfs_ownership_score=p.dfs_ownership_score,
            search_score=p.search_score,
            composite_score=p.composite_score,
            classification=p.classification.value,
            reason=p.reason,
            signals=[
                PopularitySignalOut(source=s.source, score=s.score, context=s.context)
                for s in p.signals
            ],
        )
        for p in profiles
    ]

    fade_count = sum(1 for p in profiles if p.classification == PopularityClass.FADE)
    target_count = sum(1 for p in profiles if p.classification == PopularityClass.TARGET)

    return SlatePopularityOut(
        date=slate_date.isoformat(),
        player_count=len(out_profiles),
        fade_count=fade_count,
        target_count=target_count,
        profiles=out_profiles,
    )
