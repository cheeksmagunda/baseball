"""
API router for the "Filter, Not Forecast" strategy.

This is the primary draft optimization endpoint. It implements
the full 5-filter pipeline from the Master Strategy Document.
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.core.constants import PITCHER_POSITIONS
from app.core.utils import find_player_by_name, get_trait_score
from app.models.slate import Slate, SlateGame, SlatePlayer
from app.schemas.scoring import TraitBreakdown
from app.schemas.filter_strategy import (
    FilterCard,
    FilterOptimizeRequest,
    FilterOptimizeResponse,
    FilterLineupOut,
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
    compute_pitcher_env_score,
    compute_batter_env_score,
    run_dual_filter_strategy,
)
from app.services.popularity import PopularityClass, get_popularity_profile
from app.services.pipeline import run_full_pipeline

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_game_lookup(games: list[GameEnvironment]) -> dict:
    """Build lookup dicts from game environment data."""
    game_by_id = {}
    team_to_game = {}
    for g in games:
        if g.game_id is not None:
            game_by_id[g.game_id] = g
        team_to_game[g.home_team.upper()] = g
        team_to_game[g.away_team.upper()] = g
    return game_by_id, team_to_game


async def _resolve_candidates(
    cards: list[FilterCard],
    games: list[GameEnvironment],
    db: Session,
) -> list[FilteredCandidate]:
    """
    Resolve cards into FilteredCandidates by:
    1. Looking up player in DB and scoring them
    2. Computing environmental score (Filter 2)
    3. Fetching web-scraped popularity (Filter 3)
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
            park_team = game.home_team.upper()

            k_rate_score = get_trait_score(score_result.traits, "k_rate")
            k_rate_max = next((t.max_score for t in score_result.traits if t.name == "k_rate"), 25.0)
            pitcher_k9 = (k_rate_score / k_rate_max * 12.0) if k_rate_max > 0 else None

            env_score, env_factors = compute_pitcher_env_score(
                opp_team_ops=opp_ops,
                pitcher_k_per_9=pitcher_k9,
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
            env_score = 0.5
            env_factors = ["No game environment data available"]

        # Fetch web-scraped popularity (Filter 3)
        pop_class = PopularityClass.NEUTRAL
        sharp_score = 0.0
        try:
            profile = await get_popularity_profile(
                card.player_name,
                card.team,
                score_result.total_score,
                include_sharp=True,
            )
            pop_class = profile.classification
            sharp_score = profile.sharp_score
        except Exception as exc:
            logger.warning("Popularity fetch failed for %s: %s", card.player_name, exc)

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
            popularity=pop_class,
            is_debut_or_return=card.is_debut_or_return,
            game_id=game_id,
            is_pitcher=is_pitcher,
            sharp_score=sharp_score,
            drafts=card.drafts,
            traits=score_result.traits,
        ))

    return candidates


def _load_today_slate(db: Session) -> tuple[list[FilterCard], list[GameEnvironment]]:
    """Load today's slate players and games from the database. Falls back to most recent slate if today has no data."""
    today = date.today()
    slate = (
        db.query(Slate)
        .options(
            selectinload(Slate.players).selectinload(SlatePlayer.player),
            selectinload(Slate.games),
        )
        .filter_by(date=today)
        .first()
    )

    # Fallback to most recent date if today has no data
    if not slate:
        slate = (
            db.query(Slate)
            .options(
                selectinload(Slate.players).selectinload(SlatePlayer.player),
                selectinload(Slate.games),
            )
            .order_by(Slate.date.desc())
            .first()
        )

    if not slate:
        return [], []

    games: list[GameEnvironment] = [
        GameEnvironment(
            game_id=g.id,
            home_team=g.home_team,
            away_team=g.away_team,
            vegas_total=g.vegas_total,
            home_moneyline=g.home_moneyline,
            away_moneyline=g.away_moneyline,
            home_starter=g.home_starter,
            away_starter=g.away_starter,
            home_starter_era=g.home_starter_era,
            away_starter_era=g.away_starter_era,
            home_starter_k_per_9=g.home_starter_k_per_9,
            away_starter_k_per_9=g.away_starter_k_per_9,
            wind_speed_mph=g.wind_speed_mph,
            wind_direction=g.wind_direction,
            temperature_f=g.temperature_f,
        )
        for g in slate.games
    ]

    cards: list[FilterCard] = []
    for sp in slate.players:
        player = sp.player
        if not player or sp.player_status in ("DNP", "scratched"):
            continue
        cards.append(FilterCard(
            player_name=player.name,
            team=player.team,
            position=player.position,
            card_boost=sp.card_boost,
            game_id=sp.game_id,
            batting_order=sp.batting_order,
            platoon_advantage=bool(sp.platoon_advantage),
            is_debut_or_return=sp.is_debut_or_return,
            drafts=sp.drafts,
        ))

    return cards, games


@router.post("/optimize", response_model=FilterOptimizeResponse)
async def filter_optimize(req: FilterOptimizeRequest, db: Session = Depends(get_db)):
    """
    Run the full "Filter, Not Forecast" dual-lineup pipeline.

    Returns both Starting 5 and Moonshot lineups from a single call.
    When no cards are provided, auto-loads today's slate from the database.

    Starting 5: Best filter EV with web-scraped popularity adjustments.
    Moonshot: Completely different 5 players — heavier anti-crowd lean,
              sharp signal boost, explosive trait bonus, game diversification.

    Pipeline:
    1. Classifies the slate type (tiny/pitcher_day/hitter_day/standard)
    2. Scores all players and computes environmental advantages
    3. Fetches web-scraped popularity (FADE/TARGET/NEUTRAL)
    4. Gates boosts against environmental support
    5. Enforces composition targets and game diversification
    6. Assigns players to slots with smart sequencing
    7. Builds Moonshot from remaining pool with stronger filters
    """
    cards = req.cards
    games = req.games

    if not cards:
        cards, games = _load_today_slate(db)

    # If no slate data, trigger pipeline on-demand (handles mid-slate redeploys)
    if len(cards) < 1:
        logger.info("No slate data found — triggering on-demand pipeline")
        try:
            await run_full_pipeline(db, date.today())
            cards, games = _load_today_slate(db)
        except Exception as exc:
            logger.warning("On-demand pipeline failed: %s", exc)

    if len(cards) < 1:
        raise HTTPException(404, "No slate data available for today")

    # Step 1: Classify slate (Filter 1)
    game_dicts = [g.model_dump() for g in games]
    slate_class = classify_slate(len(games), game_dicts)

    # Steps 2-3: Resolve candidates (scoring + env + popularity)
    candidates = await _resolve_candidates(cards, games, db)
    if not candidates:
        raise HTTPException(404, "No matching players found in database")

    # Steps 4-7: Run the dual filter strategy optimizer
    dual = run_dual_filter_strategy(candidates, slate_class)

    def _traits_to_breakdowns(traits: list) -> list[TraitBreakdown]:
        return [
            TraitBreakdown(trait_name=t.name, score=t.score, max_score=t.max_score, raw_value=t.raw_value)
            for t in traits
        ]

    def _build_lineup_out(result) -> FilterLineupOut:
        slots_out = [
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
                popularity=s.candidate.popularity.value,
                is_debut_or_return=s.candidate.is_debut_or_return,
                filter_ev=round(s.candidate.filter_ev, 2),
                expected_slot_value=s.expected_slot_value,
                game_id=s.candidate.game_id,
                drafts=s.candidate.drafts,
                breakdowns=_traits_to_breakdowns(s.candidate.traits),
            )
            for s in result.slots
        ]
        return FilterLineupOut(
            lineup=slots_out,
            total_expected_value=result.total_expected_value,
            strategy=result.strategy,
            composition=result.composition,
            warnings=result.warnings,
        )

    all_candidates_out = [
        FilterCandidateOut(
            player_name=c.player_name,
            team=c.team,
            position=c.position,
            card_boost=c.card_boost,
            total_score=c.total_score,
            env_score=round(c.env_score, 3),
            env_factors=c.env_factors,
            popularity=c.popularity.value,
            is_debut_or_return=c.is_debut_or_return,
            filter_ev=round(c.filter_ev, 2),
            game_id=c.game_id,
            drafts=c.drafts,
            breakdowns=_traits_to_breakdowns(c.traits),
        )
        for c in candidates
    ]

    return FilterOptimizeResponse(
        slate_classification=SlateClassificationOut(
            slate_type=dual.starting_5.slate_classification.slate_type.value,
            game_count=dual.starting_5.slate_classification.game_count,
            quality_sp_matchups=dual.starting_5.slate_classification.quality_sp_matchups,
            high_total_games=dual.starting_5.slate_classification.high_total_games,
            reason=dual.starting_5.slate_classification.reason,
        ),
        starting_5=_build_lineup_out(dual.starting_5),
        moonshot=_build_lineup_out(dual.moonshot),
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
