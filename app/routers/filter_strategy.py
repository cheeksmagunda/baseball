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
from app.core.constants import (
    DEFAULT_OPP_K_PCT,
    DEFAULT_OPP_OPS,
    DEFAULT_PITCHER_ERA,
    DEFAULT_PITCHER_WHIP,
    PITCHER_POSITIONS,
    SCORING_K9_CEILING,
    SCORING_K9_FLOOR,
)
from app.core.utils import find_player_by_name, get_trait_score
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
from app.services.popularity import PopularityClass, get_popularity_profile, reset_url_cache
from app.services.lineup_cache import lineup_cache

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_game_lookup(games: list[GameEnvironment]) -> tuple[dict, dict]:
    """Build lookup dicts from game environment data."""
    game_by_id = {}
    team_to_game = {}
    for g in games:
        if g.game_id is not None:
            game_by_id[g.game_id] = g
        team_to_game[g.home_team.upper()] = g
        team_to_game[g.away_team.upper()] = g
    return game_by_id, team_to_game


def _detect_two_way_pitcher(player, card, game) -> bool:
    """Check if a non-pitcher (e.g., DH) is the confirmed starter.

    Returns True if detected as a confirmed starter, False otherwise.
    Logs the detection when found.
    """
    if game is None:
        return False

    _is_home = game.home_team.upper() == card.team.upper()
    _starter_mlb_id = game.home_starter_mlb_id if _is_home else game.away_starter_mlb_id
    _starter_name = game.home_starter if _is_home else game.away_starter

    # Primary check: MLB ID match (authoritative, no name ambiguity)
    if _starter_mlb_id is not None and player.mlb_id == _starter_mlb_id:
        logger.info(
            "Two-way player detected: %s (%s) is confirmed starter — treating as SP",
            card.player_name, card.team,
        )
        return True

    # Fallback to name match when MLB ID wasn't returned
    if _starter_name is not None:
        _card_name = card.player_name.lower().strip()
        _prob_name = _starter_name.lower().strip()
        if _card_name in _prob_name or _prob_name in _card_name:
            logger.info(
                "Two-way player detected (name match): %s (%s) is confirmed starter — treating as SP",
                card.player_name, card.team,
            )
            return True

    return False


def _prepare_pitcher_env_kwargs(game: GameEnvironment | None, card: FilterCard) -> dict:
    """Extract pitcher environment scoring kwargs from game context."""
    score_kwargs = {}
    if game:
        _is_home = game.home_team.upper() == card.team.upper()
        _opp_ops = game.away_team_ops if _is_home else game.home_team_ops
        _opp_k_pct = game.away_team_k_pct if _is_home else game.home_team_k_pct
        if _opp_ops is not None or _opp_k_pct is not None:
            score_kwargs["opp_team_stats"] = {
                "ops": _opp_ops if _opp_ops is not None else DEFAULT_OPP_OPS,
                "k_pct": _opp_k_pct if _opp_k_pct is not None else DEFAULT_OPP_K_PCT,
            }
    return score_kwargs


def _prepare_batter_env_kwargs(game: GameEnvironment | None, card: FilterCard) -> dict:
    """Extract batter environment scoring kwargs from game context."""
    score_kwargs = {}
    if game:
        _is_home = game.home_team.upper() == card.team.upper()
        _opp_era = game.away_starter_era if _is_home else game.home_starter_era
        _opp_whip = game.away_starter_whip if _is_home else game.home_starter_whip
        if _opp_era is not None or _opp_whip is not None:
            score_kwargs["opp_pitcher_stats"] = {
                "era": _opp_era if _opp_era is not None else DEFAULT_PITCHER_ERA,
                "whip": _opp_whip if _opp_whip is not None else DEFAULT_PITCHER_WHIP,
            }
        score_kwargs["batting_order"] = card.batting_order
        score_kwargs["park_team"] = game.home_team.upper()
        score_kwargs["wind_speed_mph"] = game.wind_speed_mph
        score_kwargs["wind_direction"] = game.wind_direction
        score_kwargs["temperature_f"] = game.temperature_f
    return score_kwargs


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
    # Popularity fetchers hit several player-invariant URLs (RSS feeds, daily
    # trends). Reset the slate-wide URL cache so we deduplicate the ~125
    # candidates' fetches onto a handful of HTTP requests — and avoid serving
    # stale bodies from a previous slate run.
    reset_url_cache()

    game_by_id, team_to_game = _build_game_lookup(games)

    # Stage -1: filter out pre-game scratches only.  Draft counts come from
    # the Real Sports platform leaderboard and are only available after
    # manual ingest, so live slates routinely have cards with drafts=None.
    # Those cards now flow through — the condition classifier maps
    # drafts=None to the "medium" neutral tier, and ranking falls back onto
    # popularity, env, and trait signals (see get_ownership_tier).
    cards = [c for c in cards if c.player_name]

    # Stage 0: map cards to Player records. All cards come from the pipeline's
    # _load_active_slate, which derives them from SlatePlayer → Player rows
    # already in the DB. A missing player is a pipeline data integrity error.
    card_player_map: dict = {}
    for card in cards:
        player = find_player_by_name(db, card.player_name, card.team)
        if not player:
            raise ValueError(
                f"Player {card.player_name!r} ({card.team}) not found in database — "
                "pipeline data integrity error"
            )
        card_player_map[f"{card.player_name}|{card.team}"] = player

    # Stage 1: synchronous work (DB lookups, scoring, env score)
    pre_candidates = []
    for card in cards:
        player = card_player_map[f"{card.player_name}|{card.team}"]

        is_pitcher = player.position in PITCHER_POSITIONS

        # Find game context
        game = None
        if card.game_id is not None:
            game = game_by_id.get(card.game_id)
        if game is None:
            game = team_to_game.get(card.team.upper())

        # Two-way player detection: if a non-pitcher is the confirmed
        # starter for their game, treat them as a pitcher (e.g., Ohtani as DH).
        is_two_way_pitcher = False
        if not is_pitcher:
            is_two_way_pitcher = _detect_two_way_pitcher(player, card, game)
            if is_two_way_pitcher:
                is_pitcher = True

        # Build game-aware scoring context. Without this, batters default to
        # neutral scores on lineup_position, matchup_quality, and ballpark_factor,
        # causing unboosted pitchers (whose ERA/K-rate come from season stats) to
        # systematically outscore boosted batters regardless of matchup or order.
        if is_pitcher:
            score_kwargs = _prepare_pitcher_env_kwargs(game, card)
        else:
            score_kwargs = _prepare_batter_env_kwargs(game, card)

        score_result = score_player(db, player, is_pitcher=is_pitcher, **score_kwargs)

        # Series/momentum context — populated only for batters below
        series_team_w: int | None = None
        series_opp_w: int | None = None
        team_l10: int | None = None

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
            # Reverse the scoring engine's linear K/9 scale (floor → 0 pts, ceiling → max pts).
            k9_range = SCORING_K9_CEILING - SCORING_K9_FLOOR
            pitcher_k9 = (SCORING_K9_FLOOR + k_rate_score / k_rate_max * k9_range) if k_rate_max > 0 else None

            # V8.0: pass moneyline for Win bonus probability scoring
            team_ml = game.home_moneyline if is_home else game.away_moneyline

            env_score, env_factors = compute_pitcher_env_score(
                opp_team_ops=opp_ops,
                opp_team_k_pct=opp_k_pct,
                pitcher_k_per_9=pitcher_k9,
                park_team=park_team,
                is_home=is_home,
                team_moneyline=team_ml,
            )
            env_unknown_count = 0  # pitchers are confirmed starters; env data is reliable
        elif not is_pitcher and game:
            is_home = game.home_team.upper() == card.team.upper()
            opp_era = game.away_starter_era if is_home else game.home_starter_era
            park_team = game.home_team.upper()
            # pass team's moneyline for favorite detection
            team_ml = game.home_moneyline if is_home else game.away_moneyline

            # Bullpen vulnerability: the opposing team's bullpen ERA
            opp_bp_era = game.away_bullpen_era if is_home else game.home_bullpen_era

            # Series/momentum context for Group D env scoring
            series_team_w = game.series_home_wins if is_home else game.series_away_wins
            series_opp_w = game.series_away_wins if is_home else game.series_home_wins
            team_l10 = game.home_team_l10_wins if is_home else game.away_team_l10_wins

            env_score, env_factors, env_unknown_count = compute_batter_env_score(
                vegas_total=game.vegas_total,
                opp_pitcher_era=opp_era,
                platoon_advantage=card.platoon_advantage,
                batting_order=card.batting_order,
                park_team=park_team,
                wind_speed_mph=game.wind_speed_mph,
                wind_direction=game.wind_direction,
                temperature_f=game.temperature_f,
                team_moneyline=team_ml,
                opp_bullpen_era=opp_bp_era,
                series_team_wins=series_team_w,
                series_opp_wins=series_opp_w,
                team_l10_wins=team_l10,
            )
        else:
            raise ValueError(
                f"Player {card.player_name!r} has no associated game — "
                "env_score cannot be computed"
            )

        game_id = card.game_id
        if game_id is None and game is not None:
            game_id = game.game_id

        pre_candidates.append({
            "card": card,
            "player": player,
            "is_pitcher": is_pitcher,
            "is_two_way_pitcher": is_two_way_pitcher,
            "score_result": score_result,
            "env_score": env_score,
            "env_factors": env_factors,
            "env_unknown_count": env_unknown_count,
            "game_id": game_id,
            "series_team_wins": series_team_w,
            "series_opp_wins": series_opp_w,
            "team_l10_wins": team_l10,
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
            logger.warning(
                "Popularity fetch failed for %s — defaulting to NEUTRAL: %s",
                card.player_name, pop_result,
            )
            pop_class = PopularityClass.NEUTRAL
            sharp_score = 0.0
        else:
            pop_class = pop_result.classification
            sharp_score = pop_result.sharp_score

        candidates.append(FilteredCandidate(
            player_name=card.player_name,
            team=card.team,
            position=pre["player"].position,
            card_boost=card.card_boost,    # stored for display only
            total_score=score_result.total_score,
            env_score=pre["env_score"],
            env_factors=pre["env_factors"],
            env_unknown_count=pre.get("env_unknown_count", 0),
            popularity=pop_class,
            game_id=pre["game_id"],
            is_pitcher=pre["is_pitcher"],
            is_two_way_pitcher=pre["is_two_way_pitcher"],
            sharp_score=sharp_score,
            drafts=card.drafts,            # stored for display only
            traits=score_result.traits,
            batting_order=card.batting_order,
            series_team_wins=pre.get("series_team_wins"),
            series_opp_wins=pre.get("series_opp_wins"),
            team_l10_wins=pre.get("team_l10_wins"),
        ))

    # Candidate pool health summary (draft counts are display-only labels).
    ghost_count = sum(
        1 for c in candidates
        if c.drafts is not None and c.drafts < 100
    )
    logger.info(
        "Candidate pool: %d cards in → %d candidates out "
        "(low-draft players: %d, dropped: %d)",
        len(cards), len(candidates), ghost_count,
        len(cards) - len(candidates),
    )

    # Popularity distribution health check — detect wholesale scraper failure.
    # On any real slate, some superstars (Judge, Ohtani, etc.) will always
    # trigger at least one scraper.  If EVERY player is NEUTRAL, the scrapers
    # are broken and the 3.6x popularity differential collapses to zero.
    from app.services.popularity import PopularityClass as _PC
    fade_count = sum(1 for c in candidates if c.popularity == _PC.FADE)
    target_count = sum(1 for c in candidates if c.popularity == _PC.TARGET)
    neutral_count = sum(1 for c in candidates if c.popularity == _PC.NEUTRAL)
    logger.info(
        "Popularity distribution: FADE=%d, TARGET=%d, NEUTRAL=%d",
        fade_count, target_count, neutral_count,
    )
    if neutral_count == len(candidates) and len(candidates) >= 10:
        raise RuntimeError(
            f"All {len(candidates)} candidates classified NEUTRAL — "
            "web scraper failure suspected. The popularity signal is the "
            "primary ranking driver (3.6x batter RS differential); without "
            "it the pipeline cannot produce winning lineups. Check network "
            "connectivity and scraper endpoints."
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
            away_starter=g.away_starter,
            away_starter_mlb_id=g.away_starter_mlb_id,
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
        minutes_until_unlock = None
    elif first_pitch is not None and lock_time is not None and now < lock_time:
        phase = "before_lock"
        minutes_until_unlock = max(0, int((lock_time - now).total_seconds() / 60))
    elif first_pitch is not None:
        phase = "generating"
        minutes_until_unlock = 0
    else:
        phase = "no_slate"
        minutes_until_unlock = None

    return {
        "ready": lineup_cache.is_frozen and lineup_cache.is_warm,
        "phase": phase,
        "first_pitch_utc": first_pitch.isoformat() if first_pitch else None,
        "lock_time_utc": lock_time.isoformat() if lock_time else None,
        "minutes_until_unlock": minutes_until_unlock,
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
      - Picks cached (is_frozen): HTTP 200 immediately — no T-60 wait
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
                    "minutes_until_unlock": None,
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
                    "minutes_until_unlock": minutes_until + 5,
                },
            )

        # Past T-65 — pipeline is actively generating lineups.
        # Return 425 (not 503) so the frontend knows to retry, not give up.
        # If the pipeline actually crashed, the monitor task dies and the
        # cache never freezes — operator investigates via logs. From the
        # endpoint's perspective we cannot distinguish "running" from
        # "crashed", so we stay in "generating" until freeze succeeds.
        return JSONResponse(
            status_code=425,
            content={
                "detail": "Pipeline generating lineups — retry in a few seconds.",
                "phase": "generating",
                "first_pitch_utc": lineup_cache.first_pitch_utc.isoformat(),
                "lock_time_utc": lock_time.isoformat(),
                "minutes_until_unlock": 0,
            },
        )

    # No schedule, cache not warm → pipeline hasn't started yet
    raise HTTPException(503, "Pipeline not ready — picks will be available once the lineup is generated.")


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
    candidates = await _resolve_candidates(cards, games, db)

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
