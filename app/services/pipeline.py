"""
Daily pipeline orchestrator: fetch → score → rank.

The full pipeline is:
  1. Fetch schedule (MLB API)
  2. Fetch player stats
  3. Score all players (0-100 trait profiles)
  4. [Optional] Run filter strategy ("Filter, Not Forecast") for optimized lineups
"""

import asyncio
import logging
from datetime import date

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from app.core.constants import (
    DEFAULT_OPP_K_PCT,
    DEFAULT_OPP_OPS,
    PITCHER_POSITIONS,
    MIN_GAMES_REPRESENTED,
    is_game_remaining,
)
from app.models.player import Player, PlayerStats, normalize_name
from app.models.slate import Slate, SlateGame, SlatePlayer
from app.models.scoring import PlayerScore, ScoreBreakdown
from app.services.scoring_engine import score_player, PlayerScoreResult
from app.services.data_collection import (
    fetch_schedule_for_date,
    fetch_player_season_stats,
    populate_slate_players,
    enrich_slate_game_team_stats,
    enrich_slate_game_series_context,
    enrich_slate_game_vegas_lines,
)
from app.services.filter_strategy import (
    FilteredCandidate,
    classify_slate,
    compute_pitcher_env_score,
    compute_batter_env_score,
    run_filter_strategy,
)

logger = logging.getLogger(__name__)


def _build_starter_stats_cache(
    db: Session, games: list[SlateGame], season: int
) -> dict[str, dict]:
    """Batch-fetch stats for every probable starter on *games* in 2 SQL queries.

    Returns {starter_name: {"era", "whip", "k_per_9", "pitch_hand", "player_id"}}.
    Starters with no matching Player/PlayerStats map to an empty dict so callers
    can safely do cache.get(name, {}).get("era").
    """
    starters: list[tuple[str, str, str]] = []  # (raw_name, team, normalized_name)
    for g in games:
        if g.home_starter:
            starters.append((g.home_starter, g.home_team, normalize_name(g.home_starter)))
        if g.away_starter:
            starters.append((g.away_starter, g.away_team, normalize_name(g.away_starter)))
    if not starters:
        return {}

    unique_keys = {(norm, team) for _, team, norm in starters}
    conditions = [
        and_(Player.name_normalized == norm, Player.team == team)
        for norm, team in unique_keys
    ]
    players = db.query(Player).filter(or_(*conditions)).all()
    player_by_key = {(p.name_normalized, p.team): p for p in players}

    stats_by_pid: dict[int, PlayerStats] = {}
    if players:
        rows = (
            db.query(PlayerStats)
            .filter(PlayerStats.player_id.in_([p.id for p in players]))
            .filter(PlayerStats.season == season)
            .all()
        )
        stats_by_pid = {s.player_id: s for s in rows}

    cache: dict[str, dict] = {}
    for name, team, norm in starters:
        if name in cache:
            continue
        player = player_by_key.get((norm, team))
        if not player:
            cache[name] = {}
            continue
        ps = stats_by_pid.get(player.id)
        cache[name] = {
            "era": ps.era if ps else None,
            "whip": ps.whip if ps else None,
            "k_per_9": ps.k_per_9 if ps else None,
            "pitch_hand": player.pitch_hand,
            "player_id": player.id,
        }
    return cache


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

    # Fetch all player stats in parallel. Concurrency is capped by the
    # module-level semaphore in mlb_api._get (sem=20).
    players = [sp.player for sp in slate_players if sp.player]
    results = await asyncio.gather(
        *[fetch_player_season_stats(db, p) for p in players],
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            logger.warning("fetch_player_stats failure: %s", r)
        else:
            fetched += 1

    if players and failed >= len(players) * 0.2:
        raise RuntimeError(
            f"fetch_player_stats: {failed}/{len(players)} players failed — "
            "cannot produce a reliable lineup with more than 20% of player stats unavailable"
        )
    if failed:
        logger.critical(
            "fetch_player_stats: %d/%d players failed — lineup quality degraded, proceeding",
            failed, len(players),
        )

    # Enrich SlateGame starter ERA/K9 from newly-fetched PlayerStats.
    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    starter_cache = _build_starter_stats_cache(db, games, game_date.year)
    for game in games:
        for starter_field, era_field, whip_field, k9_field in [
            ("home_starter", "home_starter_era", "home_starter_whip", "home_starter_k_per_9"),
            ("away_starter", "away_starter_era", "away_starter_whip", "away_starter_k_per_9"),
        ]:
            starter_name = getattr(game, starter_field)
            if not starter_name or getattr(game, era_field) is not None:
                continue
            stats = starter_cache.get(starter_name, {})
            if stats.get("era") is not None:
                setattr(game, era_field, stats["era"])
            if stats.get("whip") is not None:
                setattr(game, whip_field, stats["whip"])
            if stats.get("k_per_9") is not None:
                setattr(game, k9_field, stats["k_per_9"])

    # Fetch team batting and pitching stats for all teams on the slate.
    if slate:
        await enrich_slate_game_team_stats(db, slate, game_date.year)

    # Compute platoon_advantage for each batter SlatePlayer using the starter
    # cache built above — opposing pitch_hand is already resolved there.
    games_by_id: dict[int, SlateGame] = {g.id: g for g in games}
    for sp in slate_players:
        player = sp.player
        if player is None:
            raise ValueError(
                f"SlatePlayer id={sp.id} has no linked Player — FK integrity error"
            )
        if player.position in PITCHER_POSITIONS:
            continue
        if not sp.game_id or sp.game_id not in games_by_id:
            continue
        game = games_by_id[sp.game_id]
        is_home = game.home_team == player.team
        opp_starter_name = game.away_starter if is_home else game.home_starter
        if not opp_starter_name:
            continue
        opp_pitch_hand = starter_cache.get(opp_starter_name, {}).get("pitch_hand")
        if not opp_pitch_hand or not player.bat_side:
            continue
        sp.platoon_advantage = (
            (player.bat_side == "L" and opp_pitch_hand == "R")
            or (player.bat_side == "R" and opp_pitch_hand == "L")
        )

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

    # Build game lookup and pre-cache opposing starter ERA/WHIP for batter scoring.
    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    game_lookup: dict[str, SlateGame] = {}
    for g in games:
        game_lookup[g.home_team] = g
        game_lookup[g.away_team] = g

    starter_stats_cache = _build_starter_stats_cache(db, games, game_date.year)

    # Ensure idempotency: remove any scores from a prior run of this slate.
    slate_player_ids = [sp.id for sp in slate_players]
    if slate_player_ids:
        db.query(PlayerScore).filter(
            PlayerScore.slate_player_id.in_(slate_player_ids)
        ).delete(synchronize_session=False)
        db.commit()

    results = []

    for sp in slate_players:
        player = sp.player
        if player is None:
            raise ValueError(
                f"SlatePlayer id={sp.id} has no linked Player — FK integrity error"
            )

        is_pitcher = player.position in PITCHER_POSITIONS
        game = game_lookup.get(player.team)
        if game is None:
            raise ValueError(
                f"Player {player.name!r} ({player.team}) has no associated game — "
                "pipeline data integrity error"
            )

        # Skip players whose game has already started. Belt-and-suspenders:
        # a prior failed pipeline run may have left SlatePlayer rows for
        # started games in the DB.
        if not is_game_remaining(game.game_status):
            continue

        is_home = game.home_team == player.team
        park_team = game.home_team
        opp_pitcher_stats = None
        opp_team: str | None = None
        opp_team_stats: dict | None = None
        if is_pitcher:
            opp_team = game.away_team if is_home else game.home_team
            opp_ops = game.away_team_ops if is_home else game.home_team_ops
            opp_k_pct = game.away_team_k_pct if is_home else game.home_team_k_pct
            if opp_ops is not None or opp_k_pct is not None:
                opp_team_stats = {
                    "ops": opp_ops if opp_ops is not None else DEFAULT_OPP_OPS,
                    "k_pct": opp_k_pct if opp_k_pct is not None else DEFAULT_OPP_K_PCT,
                }
        else:
            opp_starter_name = game.away_starter if is_home else game.home_starter
            if opp_starter_name:
                opp_pitcher_stats = starter_stats_cache.get(opp_starter_name)

        result = score_player(
            db, player,
            game_date=game_date,
            opp_team=opp_team,
            opp_team_stats=opp_team_stats,
            opp_pitcher_stats=opp_pitcher_stats,
            batting_order=sp.batting_order,
            park_team=park_team,
            wind_speed_mph=game.wind_speed_mph,
            wind_direction=game.wind_direction,
            temperature_f=game.temperature_f,
        )

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

    # Pre-cache opposing starter ERA/WHIP so batter matchup trait is populated.
    starter_stats_cache = _build_starter_stats_cache(db, games, game_date.year)

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
        if player is None:
            raise ValueError(
                f"SlatePlayer id={sp.id} has no linked Player — FK integrity error"
            )

        is_pitcher = player.position in PITCHER_POSITIONS

        # Find associated game
        game = game_lookup.get(player.team)
        if game is None:
            raise ValueError(
                f"Player {player.name!r} ({player.team}) has no associated game — "
                "pipeline data integrity error"
            )

        # Skip players whose game has already started. Belt-and-suspenders:
        # a prior failed pipeline run may have left SlatePlayer rows for
        # started games in the DB.
        if not is_game_remaining(game.game_status):
            continue

        is_home_p = game.home_team == player.team
        park_team = game.home_team
        opp_pitcher_stats = None
        if not is_pitcher:
            opp_starter_name = game.away_starter if is_home_p else game.home_starter
            if opp_starter_name:
                opp_pitcher_stats = starter_stats_cache.get(opp_starter_name)

        result = score_player(
            db, player,
            game_date=game_date,
            opp_pitcher_stats=opp_pitcher_stats,
            batting_order=sp.batting_order,
            park_team=park_team,
        )
        game_id = sp.game_id or game.id

        # Compute environmental score
        if is_pitcher:
            is_home = game.home_team == player.team

            # Only include the confirmed probable starter — same guard as the router.
            starter_mlb_id = game.home_starter_mlb_id if is_home else game.away_starter_mlb_id
            starter_name = game.home_starter if is_home else game.away_starter
            if starter_mlb_id is not None:
                if player.mlb_id != starter_mlb_id:
                    continue
            elif starter_name is not None:
                p_name = player.name.lower().strip()
                s_name = starter_name.lower().strip()
                if p_name not in s_name and s_name not in p_name:
                    continue

            pitcher_k9 = game.home_starter_k_per_9 if is_home else game.away_starter_k_per_9
            opp_ops = game.away_team_ops if is_home else game.home_team_ops
            opp_k_pct = game.away_team_k_pct if is_home else game.home_team_k_pct
            team_ml = game.home_moneyline if is_home else game.away_moneyline
            env_score, env_factors = compute_pitcher_env_score(
                opp_team_ops=opp_ops,
                opp_team_k_pct=opp_k_pct,
                pitcher_k_per_9=pitcher_k9,
                park_team=game.home_team,
                is_home=is_home,
                team_moneyline=team_ml,
            )
            series_team_w: int | None = None
            series_opp_w: int | None = None
            team_l10: int | None = None
        else:
            is_home = game.home_team == player.team
            opp_era = game.away_starter_era if is_home else game.home_starter_era
            team_ml = game.home_moneyline if is_home else game.away_moneyline
            opp_bp_era = game.away_bullpen_era if is_home else game.home_bullpen_era
            series_team_w: int | None = game.series_home_wins if is_home else game.series_away_wins
            series_opp_w: int | None = game.series_away_wins if is_home else game.series_home_wins
            team_l10: int | None = game.home_team_l10_wins if is_home else game.away_team_l10_wins
            env_score, env_factors, _unknown = compute_batter_env_score(
                vegas_total=game.vegas_total,
                opp_pitcher_era=opp_era,
                platoon_advantage=sp.platoon_advantage or False,
                batting_order=sp.batting_order,
                park_team=game.home_team,
                wind_speed_mph=game.wind_speed_mph,
                wind_direction=game.wind_direction,
                temperature_f=game.temperature_f,
                team_moneyline=team_ml,
                opp_bullpen_era=opp_bp_era,
                series_team_wins=series_team_w,
                series_opp_wins=series_opp_w,
                team_l10_wins=team_l10,
            )

        # Store env_score on slate player for reference
        sp.env_score = env_score
        candidates.append(FilteredCandidate(
            player_name=player.name,
            team=player.team,
            position=player.position,
            total_score=result.total_score,
            env_score=env_score,
            env_factors=env_factors,
            game_id=game_id,
            is_pitcher=is_pitcher,
            series_team_wins=series_team_w,
            series_opp_wins=series_opp_w,
            team_l10_wins=team_l10,
        ))

    db.commit()

    if not candidates:
        return {"error": "No eligible candidates found"}

    # Run the filter strategy
    lineup_result = run_filter_strategy(candidates, slate_class)

    # V10.0: card_boost lives on SlatePlayer (storage) — the optimizer does NOT
    # carry it on FilteredCandidate.  Build a display-only lookup keyed by
    # (player_name, team) so the response payload can surface it.
    boost_lookup = {
        (sp.player.name, sp.player.team): sp.card_boost
        for sp in slate_players
        if sp.player is not None
    }

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
                "boost": boost_lookup.get(
                    (s.candidate.player_name, s.candidate.team), 0.0
                ),
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

    # Mid-slate cold-start guard: when the app redeploys after the day's first
    # pitch, some games are already Live/Final. Every downstream enrichment and
    # scoring stage filters to is_game_remaining, so surface an explicit,
    # diagnosable error here if fewer than MIN_GAMES_REPRESENTED games remain.
    # Otherwise the failure would surface as a cryptic ValueError deep inside
    # _enforce_composition during the filter strategy run.
    slate = db.query(Slate).filter_by(date=game_date).first()
    if slate:
        all_games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
        remaining = [g for g in all_games if is_game_remaining(g.game_status)]
        if len(remaining) < MIN_GAMES_REPRESENTED:
            raise RuntimeError(
                f"Insufficient remaining games ({len(remaining)} of {len(all_games)}) "
                f"for {game_date} — T-65 aborted. Slate already active and too few "
                "games have yet to start."
            )

    # Clear stale roster so every T-65 run starts with a fresh snapshot.
    # Explicit cascade order because SQLite doesn't enforce FK constraints by default.
    if slate:
        sp_ids = [r for (r,) in db.query(SlatePlayer.id).filter(SlatePlayer.slate_id == slate.id)]
        if sp_ids:
            ps_ids = [r for (r,) in db.query(PlayerScore.id).filter(PlayerScore.slate_player_id.in_(sp_ids))]
            if ps_ids:
                db.query(ScoreBreakdown).filter(ScoreBreakdown.player_score_id.in_(ps_ids)).delete(synchronize_session=False)
            db.query(PlayerScore).filter(PlayerScore.slate_player_id.in_(sp_ids)).delete(synchronize_session=False)
        db.query(SlatePlayer).filter(SlatePlayer.slate_id == slate.id).delete(synchronize_session=False)
        db.commit()

    # Auto-populate SlatePlayer records from MLB API boxscores
    roster_result = {"added": 0, "skipped": 0}
    if slate:
        roster_result = await populate_slate_players(db, slate)

    stats_result = await run_fetch_player_stats(db, game_date)

    if slate:
        # Enrich series context (series wins, recent L10 form) for batter env Group D.
        # Fail loudly under the no-fallback rule — a silent failure here would
        # leave Group D signals NULL and downstream env scoring would treat
        # that as neutral, corrupting the EV formula.
        await enrich_slate_game_series_context(db, slate)

        # Enrich Vegas lines (moneyline + O/U) for pitcher/batter env scoring.
        # Raises RuntimeError if BO_ODDS_API_KEY is missing or the API fails —
        # no fallback per "no fallbacks ever" rule.
        await enrich_slate_game_vegas_lines(db, slate)

        # Enrich weather (temperature + wind) from Open-Meteo.
        # Raises RuntimeError if any game's weather cannot be fetched.
        from app.services.data_collection import enrich_slate_game_weather
        await enrich_slate_game_weather(db, slate)

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
