"""
Daily pipeline orchestrator: fetch → score → rank.
"""

from datetime import date

from sqlalchemy.orm import Session

from app.models.player import Player
from app.models.slate import Slate, SlatePlayer
from app.models.scoring import PlayerScore, ScoreBreakdown
from app.services.scoring_engine import score_player, PlayerScoreResult
from app.services.data_collection import fetch_schedule_for_date, fetch_player_season_stats


async def run_fetch(db: Session, game_date: date) -> dict:
    """Stage 1: Fetch today's schedule and create slate."""
    slate = await fetch_schedule_for_date(db, game_date)
    return {
        "date": game_date.isoformat(),
        "game_count": slate.game_count,
        "status": "fetched",
    }


async def run_fetch_player_stats(db: Session, game_date: date) -> dict:
    """Fetch stats for all players in a slate."""
    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        return {"error": "No slate found for this date"}

    slate_players = db.query(SlatePlayer).filter_by(slate_id=slate.id).all()
    fetched = 0
    failed = 0

    for sp in slate_players:
        player = db.query(Player).get(sp.player_id)
        if not player:
            continue
        try:
            await fetch_player_season_stats(db, player)
            fetched += 1
        except Exception:
            failed += 1

    return {"fetched": fetched, "failed": failed}


def run_score_slate(db: Session, game_date: date) -> list[PlayerScoreResult]:
    """Stage 2: Score all players for a slate and store results."""
    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        return []

    slate_players = db.query(SlatePlayer).filter_by(slate_id=slate.id).all()
    results = []

    for sp in slate_players:
        player = db.query(Player).get(sp.player_id)
        if not player:
            continue

        result = score_player(db, player, game_date=game_date)

        # Store in DB
        ps = PlayerScore(
            slate_player_id=sp.id,
            total_score=result.total_score,
        )
        db.add(ps)
        db.flush()

        for trait in result.traits:
            db.add(ScoreBreakdown(
                player_score_id=ps.id,
                trait_name=trait.name,
                trait_score=trait.score,
                trait_max=trait.max_score,
                raw_value=trait.raw_value,
            ))

        results.append(result)

    db.commit()

    # Sort by total score descending
    results.sort(key=lambda r: r.total_score, reverse=True)
    return results


async def run_full_pipeline(db: Session, game_date: date) -> dict:
    """Full pipeline: fetch schedule → fetch stats → score → rank."""
    fetch_result = await run_fetch(db, game_date)
    stats_result = await run_fetch_player_stats(db, game_date)
    scores = run_score_slate(db, game_date)

    return {
        "date": game_date.isoformat(),
        "schedule": fetch_result,
        "stats": stats_result,
        "scored_players": len(scores),
        "top_5": [
            {"name": s.player_name, "score": s.total_score}
            for s in scores[:5]
        ],
    }
