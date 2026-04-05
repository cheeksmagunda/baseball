"""
API router for the "Filter, Not Forecast" strategy.

This is the primary draft optimization endpoint. It implements
the full 5-filter pipeline from the Master Strategy Document.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.constants import PITCHER_POSITIONS
from app.core.utils import find_player_by_name
from app.schemas.filter_strategy import (
    FilterCard,
    FilterOptimizeRequest,
    FilterOptimizeResponse,
    FilterSlotOut,
    FilterCandidateOut,
    GameEnvironment,
    SlateClassificationOut,
)
from app.services.scoring_engine import score_player
from app.services.filter_strategy import (
    FilteredCandidate,
    SlateClassification,
    classify_slate,
    classify_ownership,
    compute_pitcher_env_score,
    compute_batter_env_score,
    run_filter_strategy,
)

router = APIRouter()


def _build_game_lookup(games: list[GameEnvironment]) -> dict:
    """Build lookup dicts from game environment data."""
    # Map game_id -> game data
    game_by_id = {}
    # Map team -> game data (for players without explicit game_id)
    team_to_game = {}
    for g in games:
        if g.game_id is not None:
            game_by_id[g.game_id] = g
        team_to_game[g.home_team.upper()] = g
        team_to_game[g.away_team.upper()] = g
    return game_by_id, team_to_game


def _resolve_candidates(
    cards: list[FilterCard],
    games: list[GameEnvironment],
    db: Session,
) -> list[FilteredCandidate]:
    """
    Resolve cards into FilteredCandidates by:
    1. Looking up player in DB and scoring them
    2. Computing environmental score (Filter 2)
    3. Classifying ownership (Filter 3)
    """
    game_by_id, team_to_game = _build_game_lookup(games)
    candidates = []

    for card in cards:
        # Score the player
        player = find_player_by_name(db, card.player_name, card.team)
        if not player:
            continue

        score_result = score_player(db, player)
        is_pitcher = player.position in PITCHER_POSITIONS

        # Find game context
        game = None
        if card.game_id is not None:
            game = game_by_id.get(card.game_id)
        if game is None:
            game = team_to_game.get(card.team.upper())

        # Compute environmental score (Filter 2)
        if is_pitcher and game:
            is_home = game.home_team.upper() == card.team.upper()
            opp_ops = game.away_team_ops if is_home else game.home_team_ops
            opp_k_pct = None  # not in game env, but could be added
            park_team = game.home_team.upper()

            env_score, env_factors = compute_pitcher_env_score(
                opp_team_ops=opp_ops,
                pitcher_k_per_9=score_result.traits[1].score / score_result.traits[1].max_score * 12.0
                if score_result.traits and len(score_result.traits) > 1 and score_result.traits[1].max_score > 0
                else None,
                park_team=park_team,
                is_home=is_home,
                is_debut_or_return=card.is_debut_or_return,
            )
        elif not is_pitcher and game:
            is_home = game.home_team.upper() == card.team.upper()
            opp_era = game.away_starter_era if is_home else game.home_starter_era
            park_team = game.home_team.upper()

            env_score, env_factors = compute_batter_env_score(
                vegas_total=game.vegas_total,
                opp_pitcher_era=opp_era,
                platoon_advantage=card.platoon_advantage,
                batting_order=card.batting_order,
                park_team=park_team,
                is_debut_or_return=card.is_debut_or_return,
            )
        else:
            # No game context: default env score
            env_score = 0.5
            env_factors = ["No game environment data available"]

        # Classify ownership (Filter 3)
        ownership = classify_ownership(card.drafts)

        game_id = card.game_id
        if game_id is None and game is not None:
            game_id = game.game_id

        candidates.append(FilteredCandidate(
            player_name=card.player_name,
            team=card.team,
            position=player.position,
            card_boost=card.card_boost,
            total_score=score_result.total_score,
            env_score=env_score,
            env_factors=env_factors,
            ownership_tier=ownership,
            is_debut_or_return=card.is_debut_or_return,
            game_id=game_id,
            is_pitcher=is_pitcher,
        ))

    return candidates


@router.post("/optimize", response_model=FilterOptimizeResponse)
def filter_optimize(req: FilterOptimizeRequest, db: Session = Depends(get_db)):
    """
    Run the full "Filter, Not Forecast" pipeline.

    This is the primary draft optimization endpoint. It:
    1. Classifies the slate type (tiny/pitcher_day/hitter_day/standard)
    2. Scores all players and computes environmental advantages
    3. Applies ownership leverage adjustments
    4. Gates boosts against environmental support
    5. Enforces composition targets and game diversification
    6. Assigns players to slots with smart sequencing

    Provide game environment data for best results. Without it,
    environmental filters default to neutral (0.5).
    """
    if len(req.cards) < 1:
        raise HTTPException(400, "Need at least 1 card")

    # Step 1: Classify slate (Filter 1)
    game_dicts = [g.model_dump() for g in req.games]
    slate_class = classify_slate(len(req.games), game_dicts)

    # Steps 2-3: Resolve candidates (scoring + env + ownership)
    candidates = _resolve_candidates(req.cards, req.games, db)
    if not candidates:
        raise HTTPException(404, "No matching players found in database")

    # Steps 4-5: Run the filter strategy optimizer
    result = run_filter_strategy(candidates, slate_class)

    # Build response
    lineup_out = [
        FilterSlotOut(
            slot_index=s.slot_index,
            slot_mult=s.slot_mult,
            player_name=s.candidate.player_name,
            team=s.candidate.team,
            position=s.candidate.position,
            card_boost=s.candidate.card_boost,
            total_score=s.candidate.total_score,
            env_score=round(s.candidate.env_score, 3),
            env_factors=s.candidate.env_factors,
            ownership_tier=s.candidate.ownership_tier.value,
            is_debut_or_return=s.candidate.is_debut_or_return,
            filter_ev=round(s.candidate.filter_ev, 2),
            expected_slot_value=s.expected_slot_value,
            game_id=s.candidate.game_id,
        )
        for s in result.slots
    ]

    all_candidates_out = [
        FilterCandidateOut(
            player_name=c.player_name,
            team=c.team,
            position=c.position,
            card_boost=c.card_boost,
            total_score=c.total_score,
            env_score=round(c.env_score, 3),
            env_factors=c.env_factors,
            ownership_tier=c.ownership_tier.value,
            is_debut_or_return=c.is_debut_or_return,
            filter_ev=round(c.filter_ev, 2),
            game_id=c.game_id,
        )
        for c in candidates
    ]

    return FilterOptimizeResponse(
        slate_classification=SlateClassificationOut(
            slate_type=result.slate_classification.slate_type.value,
            game_count=result.slate_classification.game_count,
            quality_sp_matchups=result.slate_classification.quality_sp_matchups,
            high_total_games=result.slate_classification.high_total_games,
            reason=result.slate_classification.reason,
        ),
        lineup=lineup_out,
        total_expected_value=result.total_expected_value,
        strategy=result.strategy,
        composition=result.composition,
        warnings=result.warnings,
        all_candidates=sorted(all_candidates_out, key=lambda c: c.filter_ev, reverse=True),
    )


@router.post("/classify-slate", response_model=SlateClassificationOut)
def classify_slate_endpoint(games: list[GameEnvironment] = []):
    """
    Classify a slate without running the full optimizer.

    Useful for Step 1 of the decision tree (§4.3):
    determine slate type before looking at individual players.
    """
    game_dicts = [g.model_dump() for g in games]
    result = classify_slate(len(games), game_dicts)
    return SlateClassificationOut(
        slate_type=result.slate_type.value,
        game_count=result.game_count,
        quality_sp_matchups=result.quality_sp_matchups,
        high_total_games=result.high_total_games,
        reason=result.reason,
    )
