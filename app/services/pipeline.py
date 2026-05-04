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
    PITCHER_POSITIONS,
    MIN_GAMES_REPRESENTED,
    is_game_remaining,
)
from app.core.utils import is_player_scoreable
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
    build_batter_env_kwargs,
    build_pitcher_env_kwargs,
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

    Returns {starter_name: {"era", "whip", "k_per_9", "x_era",
    "x_woba_against", "pitch_hand", "player_id"}}.  Starters with no matching
    Player/PlayerStats map to an empty dict so callers can safely do
    cache.get(name, {}).get("era").

    V10.8 — added x_era and x_woba_against (Statcast expected stats).  These
    flow from the cache into `opp_pitcher_stats` for batter scoring, where
    `score_batter_matchup` uses them as the simplified pitch-arsenal-
    effectiveness signal.
    """
    # (raw_name, team, normalized_name, mlb_id) — mlb_id is the strong key
    # (came from MLB's probablePitcher hydrate) and is the only one that
    # survives name variants like "Charlie Morton" vs "Charles Morton" or
    # accent / middle-initial drift between the schedule and roster feeds.
    starters: list[tuple[str, str, str, int | None]] = []
    for g in games:
        if g.home_starter:
            starters.append(
                (g.home_starter, g.home_team, normalize_name(g.home_starter), g.home_starter_mlb_id)
            )
        if g.away_starter:
            starters.append(
                (g.away_starter, g.away_team, normalize_name(g.away_starter), g.away_starter_mlb_id)
            )
    if not starters:
        return {}

    mlb_ids = {mid for _, _, _, mid in starters if mid is not None}
    name_team_keys = {(norm, team) for _, team, norm, _ in starters}
    name_team_conditions = [
        and_(Player.name_normalized == norm, Player.team == team)
        for norm, team in name_team_keys
    ]
    q = db.query(Player)
    if mlb_ids and name_team_conditions:
        q = q.filter(or_(Player.mlb_id.in_(mlb_ids), *name_team_conditions))
    elif mlb_ids:
        q = q.filter(Player.mlb_id.in_(mlb_ids))
    else:
        q = q.filter(or_(*name_team_conditions))
    players = q.all()
    player_by_mlb_id = {p.mlb_id: p for p in players if p.mlb_id is not None}
    player_by_name_team = {(p.name_normalized, p.team): p for p in players}

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
    for name, team, norm, mlb_id in starters:
        if name in cache:
            continue
        # Prefer mlb_id — strong key from probablePitcher hydrate.  Fall back
        # to (name_normalized, team) only when the schedule didn't expose an
        # mlb_id (very rare, but possible for late-announced starters).
        player = None
        if mlb_id is not None:
            player = player_by_mlb_id.get(mlb_id)
        if player is None:
            player = player_by_name_team.get((norm, team))
        if not player:
            cache[name] = {}
            continue
        ps = stats_by_pid.get(player.id)
        cache[name] = {
            "era": ps.era if ps else None,
            "whip": ps.whip if ps else None,
            "k_per_9": ps.k_per_9 if ps else None,
            # V10.8 expected-stats — None when no Savant row yet (rookies
            # pre-50 PA); score_batter_matchup falls through cleanly.
            "x_era": ps.x_era if ps else None,
            "x_woba_against": ps.x_woba_against if ps else None,
            "pitch_hand": player.pitch_hand,
            "player_id": player.id,
        }
    return cache


async def _refresh_statcast() -> None:
    """Bulk-load Baseball Savant leaderboards into PlayerStats.

    Runs synchronously inside run_full_pipeline between stats fetch and
    scoring. Failure raises RuntimeError — per the no-fallbacks rule,
    Statcast is a hard dependency and a failure must crash the pipeline
    so /optimize returns HTTP 503 rather than silently serving a lineup
    built without kinematics, xStats, or framing data.

    stderr output is written in addition to structured logging because
    Railway's JSON log formatter buries exc_info in an `exc` field that
    the log UI doesn't surface by default.
    """
    import sys as _sys
    import os as _os
    import traceback as _traceback

    # Ensure the project root is on sys.path so `scripts.*` is importable
    # in Railway containers where only the app sub-packages are pre-loaded.
    _proj_root = _os.path.dirname(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    )
    if _proj_root not in _sys.path:
        _sys.path.insert(0, _proj_root)

    from scripts.refresh_statcast import main as refresh_main
    from app.core.statcast import clear_statcast_cache

    clear_statcast_cache()
    logger.info("Statcast refresh: starting bulk load from Baseball Savant")
    try:
        exit_code = await asyncio.to_thread(refresh_main)
    except BaseException as exc:
        _sys.stderr.write("\n=== STATCAST REFRESH FAILED ===\n")
        _sys.stderr.write(f"Exception type: {type(exc).__name__}\n")
        _sys.stderr.write(f"Exception: {exc}\n")
        _traceback.print_exc(file=_sys.stderr)
        _sys.stderr.write("=== END STATCAST FAILURE ===\n\n")
        _sys.stderr.flush()
        raise RuntimeError(f"Statcast refresh raised an exception: {exc}") from exc

    if exit_code != 0:
        raise RuntimeError(
            f"Statcast refresh exited with code={exit_code} — inspect "
            "Baseball Savant / pybaseball column names and Player.mlb_id coverage."
        )
    logger.info("Statcast refresh complete (exit=0)")


async def run_fetch(db: Session, game_date: date) -> dict:
    """Stage 1: Fetch today's schedule and create slate."""
    logger.info("Pipeline stage 1: fetching schedule for %s", game_date)
    slate = await fetch_schedule_for_date(db, game_date)
    logger.info("Pipeline stage 1 complete: %d games on %s", slate.game_count or 0, game_date)
    return {
        "date": game_date.isoformat(),
        "game_count": slate.game_count,
        "status": "fetched",
    }


async def run_fetch_player_stats(db: Session, game_date: date) -> dict:
    """Fetch stats for all players in a slate, then backfill SlateGame starter stats."""
    logger.info("Pipeline stage 2: fetching player stats for %s", game_date)
    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        logger.warning("Pipeline stage 2: no slate found for %s", game_date)
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

    if failed:
        raise RuntimeError(
            f"fetch_player_stats: {failed}/{len(players)} players failed — "
            "pipeline cannot proceed with any missing player stats. "
            "Every player must be scored on their own data."
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

    # Strict assertion: every announced probable starter MUST have ERA/WHIP/K9
    # after enrichment, UNLESS they're flagged for the rookie track (V13.2).
    # A rookie-track pitcher is a true MLB debutant — no current-season IP and
    # no prior-season fallback hit — and is scored separately via Statcast
    # kinematics + env in scoring_engine.  Any non-rookie missing stats means
    # either (a) the Player row is absent (should be impossible after
    # _ensure_probable_starters_present), or (b) PlayerStats has no row for
    # them in this season AND the player has crossed the rookie threshold —
    # i.e. a real data-collection bug.  Fail here with the specific
    # (game, team, starter) so the gap is diagnosable from logs alone.
    # Single batched PlayerStats query keyed by player_id — was N+1 (one
    # query per slate player ≈ 200+ queries per slate).  We reuse the same
    # dict for both the pitcher rookie-track lookup below AND the batter
    # OPS gate further down.
    slate_player_ids = [sp.player.id for sp in slate_players if sp.player is not None]
    ps_by_player_id: dict[int, PlayerStats] = {}
    if slate_player_ids:
        ps_by_player_id = {
            ps.player_id: ps
            for ps in db.query(PlayerStats)
            .filter(
                PlayerStats.player_id.in_(slate_player_ids),
                PlayerStats.season == game_date.year,
            )
            .all()
        }

    starter_rookie_lookup: dict[str, bool] = {}
    for sp in slate_players:
        p = sp.player
        if p is None:
            continue
        ps_row = ps_by_player_id.get(p.id)
        if ps_row is not None:
            starter_rookie_lookup[p.name] = bool(ps_row.is_rookie_track)

    missing: list[str] = []
    for game in [g for g in games if is_game_remaining(g.game_status)]:
        for side, name_field, era_field, whip_field, k9_field in [
            ("home", "home_starter", "home_starter_era", "home_starter_whip", "home_starter_k_per_9"),
            ("away", "away_starter", "away_starter_era", "away_starter_whip", "away_starter_k_per_9"),
        ]:
            starter_name = getattr(game, name_field)
            if not starter_name:
                missing.append(
                    f"{game.away_team}@{game.home_team} {side} starter NOT ANNOUNCED"
                )
                continue
            era = getattr(game, era_field)
            whip = getattr(game, whip_field)
            k9 = getattr(game, k9_field)
            if era is None or whip is None or k9 is None:
                if starter_rookie_lookup.get(starter_name, False):
                    logger.warning(
                        "%s@%s %s starter %s on rookie scoring track "
                        "(no ERA/WHIP/K9) — will be scored on Statcast "
                        "kinematics + env only.",
                        game.away_team, game.home_team, side, starter_name,
                    )
                    continue
                missing.append(
                    f"{game.away_team}@{game.home_team} {side}={starter_name} "
                    f"era={era} whip={whip} k9={k9}"
                )
    if missing:
        raise RuntimeError(
            "Probable-starter stat enrichment incomplete after stage 2:\n  "
            + "\n  ".join(missing)
            + "\nEvery announced non-rookie starter must have ERA/WHIP/K9 in "
            "PlayerStats — no fallbacks. Investigate /people/{mlb_id} stats "
            "hydrate or active-roster vs probable-pitcher mismatch.  True "
            "rookie debutants are auto-flagged for the rookie track in "
            "fetch_player_season_stats and skip this gate."
        )

    # Strict assertion (V13.1): every RotoWire-projected batter MUST have OPS
    # in PlayerStats — anchor sub-signal for `score_offensive_profile`.  Mirrors
    # the pitcher ERA/WHIP/K9 check above.  A batter missing OPS would either
    # be silently dropped by the DNP filter (`is_player_scoreable`) or, if the
    # filter were bypassed, crash `score_offensive_profile`.  Fail here with
    # the specific gap so logs are actionable.
    #
    # Scope: only batters with `batting_order is not None`.  These are the
    # RotoWire-confirmed/expected lineup players we actually score; anyone
    # with `batting_order is None` (bench / unavailable) is excluded
    # downstream by `is_player_scoreable` regardless.  The
    # `fetch_player_season_stats` prior-season fallback should have already
    # populated OPS for IL returnees with current-season PA=0 — if we get
    # here with OPS still None, the player has no MLB hitting record at all
    # (true rookie debut), and the pipeline crashes loud per policy.
    remaining_game_ids = {
        g.id for g in games if is_game_remaining(g.game_status)
    }
    batter_missing: list[str] = []
    for sp in slate_players:
        if sp.batting_order is None:
            continue
        if sp.game_id not in remaining_game_ids:
            continue
        player = sp.player
        if player is None:
            continue
        if (player.position or "").upper() in PITCHER_POSITIONS:
            continue
        # Reuse the batched lookup built above — no per-batter SQL.
        ps = ps_by_player_id.get(player.id)
        if ps is None or ps.ops is None:
            # Rookie-track batters (true MLB debutants — no current-season + no
            # prior-season hitting record) are scored separately on Statcast
            # kinematics + env, so no OPS is required.  Any non-rookie missing
            # OPS is a real data-collection bug and still raises.
            if ps is not None and ps.is_rookie_track:
                logger.warning(
                    "%s #%s %s (mlb_id=%s) on rookie scoring track (no OPS) "
                    "— will be scored on Statcast kinematics + env only.",
                    sp.team, sp.batting_order, player.name, player.mlb_id,
                )
                continue
            ops_val = ps.ops if ps else "no_row"
            batter_missing.append(
                f"{sp.team} #{sp.batting_order} {player.name} (mlb_id={player.mlb_id}) ops={ops_val}"
            )
    if batter_missing:
        raise RuntimeError(
            "Batter OPS enrichment incomplete after stage 2:\n  "
            + "\n  ".join(batter_missing)
            + "\nEvery RotoWire-projected non-rookie batter must have OPS in "
            "PlayerStats — no fallbacks. Investigate /people/{mlb_id} stats "
            "hydrate or the prior-season hitting fallback path used for "
            "current-season PA=0 returners.  True rookies are auto-flagged "
            "for the rookie track in fetch_player_season_stats."
        )

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
    logger.info(
        "Pipeline stage 2 complete: %d stats fetched, %d failed for %s",
        fetched, failed, game_date,
    )
    return {"fetched": fetched, "failed": failed}


def _build_team_framing_lookup(
    db: Session, games: list[SlateGame], season: int
) -> dict[str, float]:
    """Return {team_abbr: framing_runs} for every team in games.

    Used by score_pitcher_k_rate's catcher-framing adjustment.  Empty dict
    when TeamSeasonStats hasn't been populated yet (first slate before
    refresh_statcast.py has run); the scoring engine falls through to no-op.
    """
    from app.models.player import TeamSeasonStats as _TSS

    teams = {g.home_team.upper() for g in games} | {g.away_team.upper() for g in games}
    if not teams:
        return {}
    result: dict[str, float] = {}
    for row in (
        db.query(_TSS)
        .filter(_TSS.team.in_(teams), _TSS.season == season)
        .all()
    ):
        if row.framing_runs is not None:
            result[row.team.upper()] = row.framing_runs
    return result


def run_score_slate(db: Session, game_date: date) -> list[PlayerScoreResult]:
    """Stage 3: Score all players for a slate and store results."""
    logger.info("Pipeline stage 3: scoring players for %s", game_date)
    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        logger.warning("Pipeline stage 3: no slate found for %s", game_date)
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

    team_framing_lookup = _build_team_framing_lookup(db, games, game_date.year)

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

        # Exclude players without enough real data to score. The same gate
        # is applied in `_load_active_slate` so the candidate pool matches.
        player_stats_row = (
            db.query(PlayerStats)
            .filter_by(player_id=player.id, season=game_date.year)
            .first()
        )
        if not is_player_scoreable(player_stats_row, is_pitcher):
            logger.info(
                "Excluding %s %s (%s): insufficient stats (PA/IP/Statcast)",
                "pitcher" if is_pitcher else "batter",
                player.name, player.team,
            )
            continue
        # Strict-mode: batters not in the projected lineup are dropped.
        if not is_pitcher and sp.batting_order is None:
            logger.info(
                "Excluding batter %s (%s): not in RotoWire-projected lineup",
                player.name, player.team,
            )
            continue

        result = score_player(
            db, player,
            game_date=game_date,
            is_pitcher=is_pitcher,
            team_framing_runs=team_framing_lookup.get(player.team.upper()),
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
    logger.info(
        "Pipeline stage 3 complete: %d players scored for %s (top: %s %.1f)",
        len(results), game_date,
        results[0].player_name if results else "n/a",
        results[0].total_score if results else 0.0,
    )
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

    team_framing_lookup = _build_team_framing_lookup(db, games, game_date.year)

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

        result = score_player(
            db, player,
            game_date=game_date,
            is_pitcher=is_pitcher,
            team_framing_runs=team_framing_lookup.get(player.team.upper()),
        )
        game_id = sp.game_id or game.id

        # Compute environmental score
        is_home = game.home_team == player.team
        series_team_w: int | None = None
        series_opp_w: int | None = None
        team_l10: int | None = None
        if is_pitcher:
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

            env_score, env_factors = compute_pitcher_env_score(
                **build_pitcher_env_kwargs(game, is_home)
            )
        else:
            env_score, env_factors, _unknown = compute_batter_env_score(
                **build_batter_env_kwargs(
                    game,
                    is_home,
                    platoon_advantage=sp.platoon_advantage or False,
                    batting_order=sp.batting_order,
                )
            )
            series_team_w = game.series_home_wins if is_home else game.series_away_wins
            series_opp_w = game.series_away_wins if is_home else game.series_home_wins
            team_l10 = game.home_team_l10_wins if is_home else game.away_team_l10_wins

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
                "score": s.candidate.total_score,
                "env_score": round(s.candidate.env_score, 3),
                "env_factors": s.candidate.env_factors,
                "filter_ev": round(s.candidate.filter_ev, 2),
                "slot_value": s.expected_slot_value,
            }
            for s in lineup_result.slots
        ],
        "candidate_count": len(candidates),
    }


async def run_full_pipeline(db: Session, game_date: date) -> dict:
    """Full pipeline: fetch schedule → populate rosters → fetch stats → score → rank.

    Mints a fresh correlation ID at entry so every log line and outbound
    HTTP request inside this T-65 fire shares the same ID — distributed
    tracing without an OpenTelemetry SDK.  The slate monitor runs as a
    background asyncio task so FastAPI middleware never sets request_id
    for it; this is the only place it gets one.  The token is reset in
    `finally` so the monitor's between-pipeline idle logs don't keep the
    stale ID.
    """
    from app.core.logging_config import request_id_var, set_pipeline_run_id

    rid, token = set_pipeline_run_id()
    try:
        logger.info("Full pipeline START for %s (correlation_id=%s)", game_date, rid)
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

        # Statcast bulk-load from Baseball Savant. Upserts kinematics + xStats
        # onto PlayerStats by mlb_id, so it must run AFTER populate_slate_players
        # + run_fetch_player_stats (which create the Player rows it keys on) and
        # BEFORE run_score_slate (which reads the columns it populates).
        # Raises RuntimeError on any failure — Savant is public + always live, so
        # a non-zero exit is a real connectivity / schema problem, not flakiness.
        await _refresh_statcast()

        scores = run_score_slate(db, game_date)
        logger.info("Full pipeline COMPLETE for %s — %d players scored", game_date, len(scores))

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
    finally:
        # Reset so the slate monitor's between-pipeline log lines don't
        # carry a stale correlation ID into the next idle period.
        request_id_var.reset(token)
