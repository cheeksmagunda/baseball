"""
API router for the "Filter, Not Forecast" strategy.

This is the primary draft optimization endpoint. It implements
the full 5-filter pipeline from the Master Strategy Document.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models.slate import Slate, SlateGame, SlatePlayer
from app.schemas.scoring import TraitBreakdown
from app.schemas.filter_strategy import (
    FilterCard,
    FilterOptimizeResponse,
    FilterLineupOut,
    FilterSlotOut,
    FilterCandidateOut,
    GameEnvironment,
    SlateClassificationOut,
    StackableGameOut,
)
from app.services.filter_strategy import (
    FilteredCandidate,
    SlateClassification,
    StackableGame,
    classify_slate,
    run_dual_filter_strategy,
)
from app.services.candidate_resolver import resolve_candidates
from app.services.lineup_cache import lineup_cache
from app.services.popularity import PopularityClass

logger = logging.getLogger(__name__)

router = APIRouter()


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
        from app.core.constants import NON_PLAYING_GAME_STATUSES
        games = db.query(SlateGame).filter_by(slate_id=today_slate.id).all()
        all_final = games and all(
            (g.home_score is not None and g.away_score is not None)
            or g.game_status in NON_PLAYING_GAME_STATUSES
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
    from app.core.constants import is_game_remaining
    remaining_games = [g for g in slate.games if is_game_remaining(g.game_status)]
    remaining_game_ids = {g.id for g in remaining_games}

    filtered_count = len(slate.games) - len(remaining_games)
    if filtered_count > 0:
        logger.warning(
            "_load_active_slate: %d of %d games already started/final for %s — "
            "only %d remaining games included in candidate pool",
            filtered_count, len(slate.games), slate_date, len(remaining_games),
        )

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
            home_starter_hand=g.home_starter_hand,
            away_starter=g.away_starter,
            away_starter_mlb_id=g.away_starter_mlb_id,
            away_starter_hand=g.away_starter_hand,
            home_starter_era=g.home_starter_era,
            away_starter_era=g.away_starter_era,
            home_starter_whip=g.home_starter_whip,
            away_starter_whip=g.away_starter_whip,
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
            series_home_wins=g.series_home_wins,
            series_away_wins=g.series_away_wins,
            home_team_l10_wins=g.home_team_l10_wins,
            away_team_l10_wins=g.away_team_l10_wins,
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
            drafts=sp.drafts,
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
            is_two_way_pitcher=s.candidate.is_two_way_pitcher,
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
            is_two_way_pitcher=c.is_two_way_pitcher,
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


async def build_and_cache_lineups(db: Session, slate_date: date | None = None) -> FilterOptimizeResponse | None:
    """
    Pre-compute today's dual-lineup result and store it in the in-process cache.

    Called by the startup pipeline so the first frontend request is instant.
    Returns the response object, or None if no slate data is available.

    Args:
        slate_date: Explicit target date. When called from the T-65 monitor,
                    pass the monitor's locked date so it cannot drift if
                    _get_active_slate_date flips mid-build.
    """
    active_date = slate_date or _get_active_slate_date(db)
    cards, games = _load_active_slate(db, active_date)
    if not cards:
        logger.warning("build_and_cache_lineups: no slate data available, skipping cache warm")
        return None

    game_dicts = [g.model_dump() for g in games]
    slate_class = classify_slate(len(games), game_dicts)

    candidates = await resolve_candidates(cards, games, db)
    if not candidates:
        logger.warning("build_and_cache_lineups: no matching players found, skipping cache warm")
        return None

    dual = run_dual_filter_strategy(candidates, slate_class)

    # run_dual_filter_strategy overwrites filter_ev on shared candidate objects
    # with moonshot EVs during Phase 2.  Re-compute S5 EVs so all_candidates
    # in the response reflects the Starting 5 ranking (what users see first).
    from app.services.filter_strategy import _compute_filter_ev
    for c in candidates:
        c.filter_ev = _compute_filter_ev(c)

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
    """
    Returns lineup cache state and timing information.

    Phases:
      no_slate      — no games scheduled today
      before_lock   — waiting for T-65 (pipeline hasn't run yet)
      generating    — T-65 has passed, pipeline is running (picks will unlock on freeze)
      ready         — picks are frozen and available
    """
    first_pitch = lineup_cache.first_pitch_utc
    lock_time = lineup_cache.lock_time_utc
    now = datetime.now(timezone.utc)

    if lineup_cache.is_frozen:
        phase = "ready"
        minutes_until_lock = None
    elif first_pitch is not None and lock_time is not None and now < lock_time:
        phase = "before_lock"
        minutes_until_lock = max(0, int((lock_time - now).total_seconds() / 60))
    elif first_pitch is not None:
        phase = "generating"
        minutes_until_lock = 0
    else:
        phase = "no_slate"
        minutes_until_lock = None

    return {
        "ready": lineup_cache.is_frozen and lineup_cache.is_warm,
        "phase": phase,
        "first_pitch_utc": first_pitch.isoformat() if first_pitch else None,
        "lock_time_utc": lock_time.isoformat() if lock_time else None,
        "minutes_until_lock": minutes_until_lock,
    }


@router.get("/optimize", response_model=FilterOptimizeResponse)
async def filter_optimize(db: Session = Depends(get_db)):
    """
    Serve frozen lineup picks from cache. Zero computation, zero API calls.

    This endpoint is read-only and never triggers the pipeline, optimizer, or
    any API work. Picks are produced EXCLUSIVELY by the T-65 slate monitor
    and locked in lineup_cache after the final run.

    T-65 Sniper Architecture:
      - Before T-65: HTTP 425 "Pipeline not yet run" (initialization phase)
      - T-65 onwards (pipeline running): HTTP 425 "Generating lineups"
      - Picks cached (is_frozen): HTTP 200 — serves frozen Redis payload
      - After all games final: HTTP 200 for the final time, then resets for next slate

    Under the "zero work outside T-65" rule, this endpoint cannot trigger any
    slate work. If T-65 has passed and no cache exists, return HTTP 503 — the
    monitor's final run failed and needs investigation (never a silent fallback).

    See CLAUDE.md § "T-65 Sniper Architecture" for complete timing model.
    """
    from fastapi.responses import JSONResponse

    # Frozen → picks are ready (freeze only fires after pipeline + cache write succeed)
    now = datetime.now(timezone.utc)
    if lineup_cache.is_frozen:
        cached = lineup_cache.get()
        if cached is not None:
            return cached

    active_date = _get_active_slate_date(db)

    # Cache warm but schedule not yet published → resolve first_pitch now
    lock_time = lineup_cache.lock_time_utc
    if lock_time is None and lineup_cache.is_warm:
        from app.services.slate_monitor import _get_first_pitch_utc

        first_pitch = _get_first_pitch_utc(db, active_date)
        if first_pitch is not None:
            lineup_cache.set_schedule(first_pitch)
            lock_time = lineup_cache.lock_time_utc
        else:
            return JSONResponse(
                status_code=425,
                content={
                    "detail": "Pipeline initializing — picks will be available once the lineup is generated.",
                    "phase": "initializing",
                    "first_pitch_utc": None,
                    "lock_time_utc": None,
                    "minutes_until_lock": None,
                },
            )

    # Schedule known → enforce T-65 gate; post-T-65 the pipeline is running
    if lock_time is not None:
        if now < lock_time:
            minutes_until = max(0, int((lock_time - now).total_seconds() / 60))
            return JSONResponse(
                status_code=425,
                content={
                    "detail": (
                        f"Pipeline runs at T-65 ({minutes_until} min). "
                        "Picks available immediately once generated."
                    ),
                    "phase": "before_lock",
                    "first_pitch_utc": lineup_cache.first_pitch_utc.isoformat(),
                    "lock_time_utc": lock_time.isoformat(),
                    "minutes_until_lock": minutes_until,
                },
            )

        # Past T-65 — check if pipeline crashed before assuming it's still running.
        if lineup_cache.pipeline_failed:
            raise HTTPException(
                503,
                "T-65 pipeline failed — picks unavailable. Check logs for the traceback.",
            )

        # Pipeline is actively generating lineups; frontend retries every 5s.
        return JSONResponse(
            status_code=425,
            content={
                "detail": "Pipeline generating lineups — retry in a few seconds.",
                "phase": "generating",
                "first_pitch_utc": lineup_cache.first_pitch_utc.isoformat(),
                "lock_time_utc": lock_time.isoformat(),
                "minutes_until_lock": 0,
            },
        )

    # No schedule, cache not warm → T-65 monitor hasn't started yet (pre-T-65 init)
    return JSONResponse(
        status_code=425,
        content={
            "detail": "Pipeline not yet run — picks will be available at T-65.",
            "phase": "initializing",
            "first_pitch_utc": None,
            "lock_time_utc": None,
            "minutes_until_lock": None,
        },
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


@router.get("/diagnostics")
async def diagnostics(db: Session = Depends(get_db)):
    """
    Pipeline health dashboard — popularity distribution, pool metrics, top EVs.

    Returns the current cached lineup's diagnostics without re-running the
    optimizer.  If no cache exists, loads slate data and computes a snapshot.
    """
    cached = lineup_cache.get()
    if cached is not None:
        candidates = cached.all_candidates
        fade = sum(1 for c in candidates if c.popularity == "FADE")
        target = sum(1 for c in candidates if c.popularity == "TARGET")
        neutral = sum(1 for c in candidates if c.popularity == "NEUTRAL")
        top_evs = [
            {"player": c.player_name, "team": c.team, "ev": c.filter_ev, "popularity": c.popularity}
            for c in candidates[:10]
        ]
        return {
            "source": "cache",
            "candidate_count": len(candidates),
            "popularity_distribution": {"FADE": fade, "TARGET": target, "NEUTRAL": neutral},
            "popularity_healthy": not (neutral == len(candidates) and len(candidates) >= 10),
            "top_10_ev": top_evs,
            "starting_5": [s.player_name for s in cached.starting_5.lineup],
            "moonshot": [s.player_name for s in cached.moonshot.lineup],
        }

    # No cache — build a snapshot from the DB
    active_date = _get_active_slate_date(db)
    cards, games = _load_active_slate(db, active_date)
    if not cards:
        return {
            "source": "live",
            "error": f"No slate data for {active_date}",
            "candidate_count": 0,
            "popularity_distribution": {"FADE": 0, "TARGET": 0, "NEUTRAL": 0},
            "popularity_healthy": False,
            "top_10_ev": [],
            "starting_5": [],
            "moonshot": [],
        }

    game_dicts = [g.model_dump() for g in games]
    slate_class = classify_slate(len(games), game_dicts)
    candidates = await resolve_candidates(cards, games, db)

    fade = sum(1 for c in candidates if c.popularity == PopularityClass.FADE)
    target = sum(1 for c in candidates if c.popularity == PopularityClass.TARGET)
    neutral = sum(1 for c in candidates if c.popularity == PopularityClass.NEUTRAL)

    # Quick EV computation for diagnostics (without full optimization)
    from app.services.filter_strategy import _compute_filter_ev
    for c in candidates:
        c.filter_ev = _compute_filter_ev(c)
    candidates.sort(key=lambda c: c.filter_ev, reverse=True)

    top_evs = [
        {"player": c.player_name, "team": c.team, "ev": round(c.filter_ev, 2), "popularity": c.popularity.value}
        for c in candidates[:10]
    ]

    return {
        "source": "live",
        "slate_date": str(active_date),
        "slate_type": slate_class.slate_type.value,
        "candidate_count": len(candidates),
        "popularity_distribution": {"FADE": fade, "TARGET": target, "NEUTRAL": neutral},
        "popularity_healthy": not (neutral == len(candidates) and len(candidates) >= 10),
        "top_10_ev": top_evs,
        "starting_5": [],
        "moonshot": [],
    }
