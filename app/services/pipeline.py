"""
Daily pipeline orchestrator: fetch → score → rank.

The full pipeline is:
  1. Fetch schedule (MLB API)
  2. Fetch player stats
  3. Score all players (0-100 trait profiles)
  4. [Optional] Run filter strategy ("Filter, Not Forecast") for optimized lineups
"""

import asyncio
from datetime import date

from sqlalchemy.orm import Session, selectinload, joinedload

from app.core.constants import PITCHER_POSITIONS
from app.models.player import Player
from app.models.slate import Slate, SlateGame, SlatePlayer
from app.models.scoring import PlayerScore, ScoreBreakdown
from app.services.scoring_engine import score_player, PlayerScoreResult
from app.services.data_collection import fetch_schedule_for_date, fetch_player_season_stats, populate_slate_players
from app.services.filter_strategy import (
    FilteredCandidate,
    classify_slate,
    compute_pitcher_env_score,
    compute_batter_env_score,
    run_filter_strategy,
)


async def run_fetch(db: Session, game_date: date) -> dict:
    """Stage 1: Fetch today's schedule and create slate."""
    slate = await fetch_schedule_for_date(db, game_date)
    return {
        "date": game_date.isoformat(),
        "game_count": slate.game_count,
        "status": "fetched",
    }


async def run_fetch_player_stats(db: Session, game_date: date) -> dict:
    """Fetch stats for all players in a slate, then backfill SlateGame starter stats."""
    from app.models.player import PlayerStats
    import logging
    logger = logging.getLogger(__name__)

    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        return {"error": "No slate found for this date"}

    slate_players = (
        db.query(SlatePlayer)
        .options(joinedload(SlatePlayer.player))
        .filter_by(slate_id=slate.id)
        .all()
    )
    fetched = 0
    failed = 0

    # Fetch all player stats in parallel (HTTP is async; DB ops serialize naturally
    # since asyncio is single-threaded and yields only at await points).
    _SEM = asyncio.Semaphore(20)  # cap concurrent MLB API connections

    async def _fetch(player):
        async with _SEM:
            return await fetch_player_season_stats(db, player)

    players = [sp.player for sp in slate_players if sp.player]
    results = await asyncio.gather(*[_fetch(p) for p in players], return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            failed += 1
        else:
            fetched += 1

    # Backfill SlateGame starter ERA/K9 from newly-fetched PlayerStats.
    # This feeds the environmental scoring engine (Filter 2).
    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    for game in games:
        for starter_field, era_field, k9_field, team_field in [
            ("home_starter", "home_starter_era", "home_starter_k_per_9", "home_team"),
            ("away_starter", "away_starter_era", "away_starter_k_per_9", "away_team"),
        ]:
            starter_name = getattr(game, starter_field)
            if not starter_name:
                continue
            if getattr(game, era_field) is not None:
                continue  # already populated

            team = getattr(game, team_field)
            from app.models.player import normalize_name
            norm = normalize_name(starter_name)
            player = db.query(Player).filter_by(name_normalized=norm, team=team).first()
            if not player:
                continue

            ps = (
                db.query(PlayerStats)
                .filter_by(player_id=player.id, season=game_date.year)
                .first()
            )
            if not ps:
                continue

            if ps.era is not None:
                setattr(game, era_field, ps.era)
            if ps.k_per_9 is not None:
                setattr(game, k9_field, ps.k_per_9)

    db.commit()
    return {"fetched": fetched, "failed": failed}


def run_score_slate(db: Session, game_date: date) -> list[PlayerScoreResult]:
    """Stage 2: Score all players for a slate and store results."""
    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        return []

    slate_players = (
        db.query(SlatePlayer)
        .options(joinedload(SlatePlayer.player))
        .filter_by(slate_id=slate.id)
        .all()
    )
    results = []

    for sp in slate_players:
        player = sp.player
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


def run_filter_strategy_from_slate(db: Session, game_date: date) -> dict:
    """
    Run the "Filter, Not Forecast" pipeline using slate data from DB.

    Uses stored SlateGame environmental data and SlatePlayer pre-game fields
    to produce filter-optimized lineups. This is the recommended optimization
    path after scoring is complete.
    """
    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        return {"error": "No slate found for this date"}

    # Build game environment data from SlateGame records
    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    game_dicts = []
    game_lookup = {}
    for g in games:
        gd = {
            "game_id": g.id,
            "home_team": g.home_team,
            "away_team": g.away_team,
            "vegas_total": g.vegas_total,
            "home_starter_era": g.home_starter_era,
            "away_starter_era": g.away_starter_era,
            "home_starter_k_per_9": g.home_starter_k_per_9,
            "away_starter_k_per_9": g.away_starter_k_per_9,
        }
        game_dicts.append(gd)
        game_lookup[g.home_team] = g
        game_lookup[g.away_team] = g

    # Classify the slate (Filter 1)
    slate_class = classify_slate(len(games), game_dicts)

    # Build candidates from slate players
    slate_players = (
        db.query(SlatePlayer)
        .options(joinedload(SlatePlayer.player))
        .filter_by(slate_id=slate.id)
        .all()
    )
    candidates = []

    for sp in slate_players:
        if sp.player_status in ("DNP", "scratched"):
            continue

        player = sp.player
        if not player:
            continue

        result = score_player(db, player, game_date=game_date)
        is_pitcher = player.position in PITCHER_POSITIONS

        # Find associated game
        game = game_lookup.get(player.team)
        game_id = sp.game_id or (game.id if game else None)

        # Compute environmental score
        if is_pitcher and game:
            is_home = game.home_team == player.team
            # Get pitcher K/9 from game environment data if available
            pitcher_k9 = game.home_starter_k_per_9 if is_home else game.away_starter_k_per_9
            env_score, env_factors = compute_pitcher_env_score(
                opp_team_ops=None,  # would need team stats
                pitcher_k_per_9=pitcher_k9,
                park_team=game.home_team,
                is_home=is_home,
                is_debut_or_return=sp.is_debut_or_return,
            )
        elif not is_pitcher and game:
            is_home = game.home_team == player.team
            opp_era = game.away_starter_era if is_home else game.home_starter_era
            env_score, env_factors = compute_batter_env_score(
                vegas_total=game.vegas_total,
                opp_pitcher_era=opp_era,
                platoon_advantage=sp.platoon_advantage or False,
                batting_order=sp.batting_order,
                park_team=game.home_team,
                is_debut_or_return=sp.is_debut_or_return,
            )
        else:
            env_score = 0.5
            env_factors = []

        # Store env_score on slate player for reference
        sp.env_score = env_score
        candidates.append(FilteredCandidate(
            player_name=player.name,
            team=player.team,
            position=player.position,
            card_boost=sp.card_boost,
            total_score=result.total_score,
            env_score=env_score,
            env_factors=env_factors,
            is_debut_or_return=sp.is_debut_or_return,
            game_id=game_id,
            is_pitcher=is_pitcher,
        ))

    db.commit()

    if not candidates:
        return {"error": "No eligible candidates found"}

    # Run the filter strategy
    lineup_result = run_filter_strategy(candidates, slate_class)

    return {
        "date": game_date.isoformat(),
        "slate_type": slate_class.slate_type.value,
        "slate_reason": slate_class.reason,
        "composition": lineup_result.composition,
        "total_expected_value": lineup_result.total_expected_value,
        "warnings": lineup_result.warnings,
        "lineup": [
            {
                "slot": s.slot_index,
                "slot_mult": s.slot_mult,
                "player": s.candidate.player_name,
                "team": s.candidate.team,
                "position": s.candidate.position,
                "boost": s.candidate.card_boost,
                "score": s.candidate.total_score,
                "env_score": round(s.candidate.env_score, 3),
                "env_factors": s.candidate.env_factors,
                "popularity": s.candidate.popularity.value,
                "filter_ev": round(s.candidate.filter_ev, 2),
                "slot_value": s.expected_slot_value,
            }
            for s in lineup_result.slots
        ],
        "candidate_count": len(candidates),
    }


async def run_full_pipeline(db: Session, game_date: date) -> dict:
    """Full pipeline: fetch schedule → populate rosters → fetch stats → score → rank."""
    fetch_result = await run_fetch(db, game_date)

    # Auto-populate SlatePlayer records from MLB API boxscores
    slate = db.query(Slate).filter_by(date=game_date).first()
    roster_result = {"added": 0, "skipped": 0}
    if slate:
        roster_result = await populate_slate_players(db, slate)

    stats_result = await run_fetch_player_stats(db, game_date)
    scores = run_score_slate(db, game_date)

    return {
        "date": game_date.isoformat(),
        "schedule": fetch_result,
        "rosters": roster_result,
        "stats": stats_result,
        "scored_players": len(scores),
        "top_5": [
            {"name": s.player_name, "score": s.total_score}
            for s in scores[:5]
        ],
    }
