"""
API router for the "Filter, Not Forecast" strategy.

This is the primary draft optimization endpoint. It implements
the full 5-filter pipeline from the Master Strategy Document.
"""

import asyncio
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.core.constants import PITCHER_POSITIONS, MOST_DRAFTED_3X_TOP_N
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
    StackableGameOut,
)
from app.services.scoring_engine import score_player
from app.services.filter_strategy import (
    FilteredCandidate,
    SlateClassification,
    StackableGame,
    classify_slate,
    compute_pitcher_env_score,
    compute_batter_env_score,
    run_dual_filter_strategy,
)
from app.services.popularity import PopularityClass, get_popularity_profile
from app.services.pipeline import run_full_pipeline
from app.services.lineup_cache import lineup_cache

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
    3. Fetching web-scraped popularity (Filter 3) — all players in parallel
    """
    game_by_id, team_to_game = _build_game_lookup(games)

    # Stage 1: synchronous work (DB lookups, scoring, env score)
    pre_candidates = []
    for card in cards:
        player = find_player_by_name(db, card.player_name, card.team)
        if not player:
            continue

        is_pitcher = player.position in PITCHER_POSITIONS

        # Find game context
        game = None
        if card.game_id is not None:
            game = game_by_id.get(card.game_id)
        if game is None:
            game = team_to_game.get(card.team.upper())

        # Build game-aware scoring context. Without this, batters default to
        # neutral scores on lineup_position, matchup_quality, and ballpark_factor,
        # causing unboosted pitchers (whose ERA/K-rate come from season stats) to
        # systematically outscore boosted batters regardless of matchup or order.
        score_kwargs: dict = {}
        if game:
            _is_home = game.home_team.upper() == card.team.upper()
            if is_pitcher:
                _opp_ops = game.away_team_ops if _is_home else game.home_team_ops
                _opp_k_pct = game.away_team_k_pct if _is_home else game.home_team_k_pct
                if _opp_ops is not None or _opp_k_pct is not None:
                    score_kwargs["opp_team_stats"] = {
                        "ops": _opp_ops if _opp_ops is not None else 0.730,
                        "k_pct": _opp_k_pct if _opp_k_pct is not None else 0.22,
                    }
            else:
                _opp_era = game.away_starter_era if _is_home else game.home_starter_era
                if _opp_era is not None:
                    score_kwargs["opp_pitcher_stats"] = {"era": _opp_era}
                score_kwargs["batting_order"] = card.batting_order
                score_kwargs["park_team"] = game.home_team.upper()
                # Pass weather data for dynamic park factor adjustment
                score_kwargs["wind_speed_mph"] = game.wind_speed_mph
                score_kwargs["wind_direction"] = game.wind_direction
                score_kwargs["temperature_f"] = game.temperature_f

        score_result = score_player(db, player, **score_kwargs)

        # Compute environmental score (Filter 2)
        if is_pitcher and game:
            is_home = game.home_team.upper() == card.team.upper()

            # Only include the confirmed probable starter for this game.
            # Pitchers on rest from the previous day are still on the active
            # roster and score well — they must be excluded here.
            # Primary check: MLB ID match (authoritative, no name ambiguity).
            # Fallback to name only when the ID wasn't returned by the API.
            starter_mlb_id = game.home_starter_mlb_id if is_home else game.away_starter_mlb_id
            starter_name = game.home_starter if is_home else game.away_starter
            if starter_mlb_id is not None:
                if player.mlb_id != starter_mlb_id:
                    continue
            elif starter_name is not None:
                card_name = card.player_name.lower().strip()
                prob_name = starter_name.lower().strip()
                if card_name not in prob_name and prob_name not in card_name:
                    continue

            opp_ops = game.away_team_ops if is_home else game.home_team_ops
            opp_k_pct = game.away_team_k_pct if is_home else game.home_team_k_pct
            park_team = game.home_team.upper()

            k_rate_score = get_trait_score(score_result.traits, "k_rate")
            k_rate_max = next((t.max_score for t in score_result.traits if t.name == "k_rate"), 25.0)
            # The scoring engine maps K/9 linearly: 6.0 K/9 → 0 pts, 12.0 K/9 → max pts.
            # Reverse: K/9 = 6.0 + (score/max) * 6.0.  The old formula (score/max * 12)
            # ignored the 6.0 floor, compressing a 10 K/9 pitcher down to 8.0 K/9.
            pitcher_k9 = (6.0 + k_rate_score / k_rate_max * 6.0) if k_rate_max > 0 else None

            env_score, env_factors = compute_pitcher_env_score(
                opp_team_ops=opp_ops,
                opp_team_k_pct=opp_k_pct,
                pitcher_k_per_9=pitcher_k9,
                park_team=park_team,
                is_home=is_home,
                is_debut_or_return=card.is_debut_or_return,
            )
        elif not is_pitcher and game:
            is_home = game.home_team.upper() == card.team.upper()
            opp_era = game.away_starter_era if is_home else game.home_starter_era
            park_team = game.home_team.upper()
            # V2: pass team's moneyline for favorite detection
            team_ml = game.home_moneyline if is_home else game.away_moneyline

            # Bullpen vulnerability: the opposing team's bullpen ERA
            opp_bp_era = game.away_bullpen_era if is_home else game.home_bullpen_era

            env_score, env_factors = compute_batter_env_score(
                vegas_total=game.vegas_total,
                opp_pitcher_era=opp_era,
                platoon_advantage=card.platoon_advantage,
                batting_order=card.batting_order,
                park_team=park_team,
                is_debut_or_return=card.is_debut_or_return,
                wind_speed_mph=game.wind_speed_mph,
                wind_direction=game.wind_direction,
                temperature_f=game.temperature_f,
                team_moneyline=team_ml,
                opp_bullpen_era=opp_bp_era,
            )
        else:
            env_score = 0.5
            env_factors = ["No game environment data available"]

        game_id = card.game_id
        if game_id is None and game is not None:
            game_id = game.game_id

        pre_candidates.append({
            "card": card,
            "player": player,
            "is_pitcher": is_pitcher,
            "score_result": score_result,
            "env_score": env_score,
            "env_factors": env_factors,
            "game_id": game_id,
        })

    # Stage 2: fetch popularity for all players in parallel (Filter 3)
    popularity_results = await asyncio.gather(
        *[
            get_popularity_profile(
                p["card"].player_name,
                p["card"].team,
                p["score_result"].total_score,
                include_sharp=True,
            )
            for p in pre_candidates
        ],
        return_exceptions=True,
    )

    # Stage 3: assemble FilteredCandidates
    candidates = []
    for pre, pop_result in zip(pre_candidates, popularity_results):
        card = pre["card"]
        score_result = pre["score_result"]

        if isinstance(pop_result, Exception):
            logger.warning("Popularity fetch failed for %s — defaulting to NEUTRAL: %s", card.player_name, pop_result)
            pop_class = PopularityClass.NEUTRAL
            sharp_score = 0.0
        else:
            pop_class = pop_result.classification
            sharp_score = pop_result.sharp_score

        candidates.append(FilteredCandidate(
            player_name=card.player_name,
            team=card.team,
            position=pre["player"].position,
            card_boost=card.card_boost,
            total_score=score_result.total_score,
            env_score=pre["env_score"],
            env_factors=pre["env_factors"],
            popularity=pop_class,
            is_debut_or_return=card.is_debut_or_return,
            game_id=pre["game_id"],
            is_pitcher=pre["is_pitcher"],
            sharp_score=sharp_score,
            drafts=card.drafts,
            is_most_drafted_3x=card.is_most_drafted_3x,
            traits=score_result.traits,
            batting_order=card.batting_order,
        ))

    # Dynamic is_most_drafted_3x: the DB flag is only set retrospectively by post-game
    # analysis and is always False for today's live slate.  Compute it on the fly:
    # mark the top-N most-drafted players with boost >= 3.0 so the V2.3 env-aware
    # trap penalty actually fires.  Matches the ~5-per-day historical pattern.
    boost3_by_drafts = sorted(
        [c for c in candidates if c.card_boost >= 3.0 and c.drafts is not None],
        key=lambda c: c.drafts,
        reverse=True,
    )
    for c in boost3_by_drafts[:MOST_DRAFTED_3X_TOP_N]:
        c.is_most_drafted_3x = True
        logger.debug(
            "Dynamic is_most_drafted_3x: %s (drafts=%s, boost=%.1f)",
            c.player_name, c.drafts, c.card_boost,
        )

    return candidates


def _get_active_slate_date(db: Session) -> date:
    """
    Determine the correct slate date to serve.

    Returns today if today has an active slate with unfinished games.
    Returns tomorrow if today's slate is empty/nonexistent/complete.
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)

    today_slate = db.query(Slate).filter_by(date=today).first()

    # Today has games — check if any are still in progress
    if today_slate and today_slate.game_count and today_slate.game_count > 0:
        games = db.query(SlateGame).filter_by(slate_id=today_slate.id).all()
        all_final = games and all(
            g.home_score is not None and g.away_score is not None
            for g in games
        )
        if not all_final:
            return today
        # All games final — fall through to serve tomorrow

    # Today is empty, nonexistent, or complete — serve tomorrow
    return tomorrow


def _load_active_slate(db: Session, slate_date: date | None = None) -> tuple[list[FilterCard], list[GameEnvironment]]:
    """Load the active slate's players and games from the database."""
    if slate_date is None:
        slate_date = _get_active_slate_date(db)
    slate = (
        db.query(Slate)
        .options(
            selectinload(Slate.players).selectinload(SlatePlayer.player),
            selectinload(Slate.games),
        )
        .filter_by(date=slate_date)
        .first()
    )
    if not slate:
        return [], []

    # Filter out games that have already started (Live) or finished (Final).
    # Games with NULL game_status are treated as not-yet-started (safe default).
    # None is not in STARTED_STATUSES, so null-status games pass through.
    STARTED_STATUSES = {"Live", "Final"}
    remaining_games = [
        g for g in slate.games
        if g.game_status not in STARTED_STATUSES
    ]
    remaining_game_ids = {g.id for g in remaining_games}

    games: list[GameEnvironment] = [
        GameEnvironment(
            game_id=g.id,
            home_team=g.home_team,
            away_team=g.away_team,
            vegas_total=g.vegas_total,
            home_moneyline=g.home_moneyline,
            away_moneyline=g.away_moneyline,
            home_starter=g.home_starter,
            home_starter_mlb_id=g.home_starter_mlb_id,
            away_starter=g.away_starter,
            away_starter_mlb_id=g.away_starter_mlb_id,
            home_starter_era=g.home_starter_era,
            away_starter_era=g.away_starter_era,
            home_starter_k_per_9=g.home_starter_k_per_9,
            away_starter_k_per_9=g.away_starter_k_per_9,
            home_team_ops=g.home_team_ops,
            away_team_ops=g.away_team_ops,
            home_team_k_pct=g.home_team_k_pct,
            away_team_k_pct=g.away_team_k_pct,
            wind_speed_mph=g.wind_speed_mph,
            wind_direction=g.wind_direction,
            temperature_f=g.temperature_f,
            home_bullpen_era=g.home_bullpen_era,
            away_bullpen_era=g.away_bullpen_era,
        )
        for g in remaining_games
    ]

    cards: list[FilterCard] = []
    for sp in slate.players:
        player = sp.player
        if not player or sp.player_status in ("DNP", "scratched"):
            continue
        # Skip players from games that have already started
        if sp.game_id is not None and sp.game_id not in remaining_game_ids:
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
            is_most_drafted_3x=sp.is_most_drafted_3x,
        ))

    return cards, games


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


def _build_response(dual, candidates) -> FilterOptimizeResponse:
    """Assemble the FilterOptimizeResponse from a dual-lineup result + candidate list."""
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
    sc = dual.starting_5.slate_classification
    stackable_out = [
        StackableGameOut(
            game_id=sg.game_id,
            favored_team=sg.favored_team,
            moneyline=sg.moneyline,
            vegas_total=sg.vegas_total,
            opp_starter_era=sg.opp_starter_era,
        )
        for sg in sc.stackable_games
    ]
    return FilterOptimizeResponse(
        slate_classification=SlateClassificationOut(
            slate_type=sc.slate_type.value,
            game_count=sc.game_count,
            quality_sp_matchups=sc.quality_sp_matchups,
            high_total_games=sc.high_total_games,
            blowout_games=sc.blowout_games,
            stackable_games=stackable_out,
            reason=sc.reason,
        ),
        starting_5=_build_lineup_out(dual.starting_5),
        moonshot=_build_lineup_out(dual.moonshot),
        all_candidates=sorted(all_candidates_out, key=lambda c: c.filter_ev, reverse=True),
    )


async def build_and_cache_lineups(db: Session) -> FilterOptimizeResponse | None:
    """
    Pre-compute today's dual-lineup result and store it in the in-process cache.

    Called by the startup pipeline so the first frontend request is instant.
    Returns the response object, or None if no slate data is available.
    """
    active_date = _get_active_slate_date(db)
    cards, games = _load_active_slate(db, active_date)
    if not cards:
        logger.warning("build_and_cache_lineups: no slate data available, skipping cache warm")
        return None

    game_dicts = [g.model_dump() for g in games]
    slate_class = classify_slate(len(games), game_dicts)

    candidates = await _resolve_candidates(cards, games, db)
    if not candidates:
        logger.warning("build_and_cache_lineups: no matching players found, skipping cache warm")
        return None

    dual = run_dual_filter_strategy(candidates, slate_class)
    response = _build_response(dual, candidates)
    lineup_cache.store(response, slate_date=active_date)
    logger.info(
        "Lineup cache warmed: %d candidates, slate=%s",
        len(candidates),
        slate_class.slate_type.value,
    )
    return response


@router.get("/status")
def optimize_status():
    """Returns whether the lineup cache is warm (startup pipeline complete)."""
    return {"ready": lineup_cache.is_warm}


@router.post("/optimize", response_model=FilterOptimizeResponse)
async def filter_optimize(req: FilterOptimizeRequest, db: Session = Depends(get_db)):
    """
    Run the full "Filter, Not Forecast" dual-lineup pipeline.

    Returns both Starting 5 and Moonshot lineups from a single call.
    When no cards are provided, serves from the in-process cache that the
    startup pipeline pre-computes — so the response is instant.

    Starting 5: Best filter EV with web-scraped popularity adjustments.
    Moonshot: Completely different 5 players — heavier anti-crowd lean,
              sharp signal boost, explosive trait bonus, game diversification.
    """
    cards = req.cards
    games = req.games

    # Fast path: serve pre-computed result from cache when no custom cards given
    if not cards:
        cached = lineup_cache.get()
        if cached is not None:
            return cached

    if not cards:
        active_date = _get_active_slate_date(db)
        cards, games = _load_active_slate(db, active_date)

    # If no slate data, trigger pipeline on-demand (handles mid-slate redeploys)
    if len(cards) < 1:
        active_date = _get_active_slate_date(db)
        logger.info("No slate data found — triggering on-demand pipeline for %s", active_date)
        try:
            await run_full_pipeline(db, active_date)
            cards, games = _load_active_slate(db, active_date)
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
    response = _build_response(dual, candidates)

    # Cache the result for subsequent frontend requests
    if not req.cards:
        lineup_cache.store(response, slate_date=_get_active_slate_date(db))

    return response


@router.post("/classify-slate", response_model=SlateClassificationOut)
def classify_slate_endpoint(games: list[GameEnvironment] = []):
    """
    Classify a slate without running the full optimizer.

    Useful for Step 1 of the decision tree (§4.3):
    determine slate type before looking at individual players.
    """
    game_dicts = [g.model_dump() for g in games]
    result = classify_slate(len(games), game_dicts)
    stackable_out = [
        StackableGameOut(
            game_id=sg.game_id,
            favored_team=sg.favored_team,
            moneyline=sg.moneyline,
            vegas_total=sg.vegas_total,
            opp_starter_era=sg.opp_starter_era,
        )
        for sg in result.stackable_games
    ]
    return SlateClassificationOut(
        slate_type=result.slate_type.value,
        game_count=result.game_count,
        quality_sp_matchups=result.quality_sp_matchups,
        high_total_games=result.high_total_games,
        blowout_games=result.blowout_games,
        stackable_games=stackable_out,
        reason=result.reason,
    )
