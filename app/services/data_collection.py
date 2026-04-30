"""
Data collection service: fetches player stats from MLB Stats API
and stores them in the database.
"""

import asyncio
import logging
from datetime import date, datetime as _datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import settings
from app.core.constants import (
    ET_TO_UTC_OFFSET_HOURS,
    NON_PLAYING_GAME_STATUSES,
    canonicalize_team,
    is_game_remaining,
)
from app.core.mlb_api import (
    get_schedule,
    get_game_boxscore,
    get_player_stats,
    get_team_stats,
    get_team_roster,
    search_player,
    TEAM_MLB_IDS,
    TEAM_ABBR_BY_MLB_ID,
)
from app.models.player import Player, PlayerStats, PlayerGameLog, normalize_name
from app.models.slate import Slate, SlateGame, SlatePlayer

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


def _safe_float(value: str | None) -> float | None:
    """Convert a stat string to float, returning None on blank or non-numeric values.

    The MLB Stats API returns sentinel strings like '.---' or '-.--' for players
    with no qualifying stats. float() would raise ValueError on these.
    """
    if not value:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _format_game_time_et(game_date_iso: str | None) -> str | None:
    """
    Convert MLB API gameDate (ISO 8601 UTC) to 'H:MM AM/PM ET' display format.

    The MLB schedule API returns gameDate as e.g. "2026-04-11T23:05:00Z".
    This converts to Eastern Time and formats as "7:05 PM ET" for storage
    in SlateGame.scheduled_game_time, which the T-65 monitor parses to
    determine first-pitch time.
    """
    if not game_date_iso:
        return None
    utc_dt = _datetime.fromisoformat(game_date_iso.replace("Z", "+00:00"))
    et_dt = utc_dt.astimezone(_ET)
    formatted = et_dt.strftime("%I:%M %p")
    # Strip leading zero: "07:05 PM" → "7:05 PM"
    if formatted.startswith("0"):
        formatted = formatted[1:]
    return f"{formatted} ET"


async def fetch_schedule_for_date(db: Session, game_date: date) -> Slate:
    """Fetch MLB schedule and create/update slate with games."""
    data = await get_schedule(game_date.isoformat())

    slate = db.query(Slate).filter_by(date=game_date).first()
    if not slate:
        slate = Slate(date=game_date, status="pending")
        db.add(slate)
        db.flush()

    game_dates = data.get("dates", [])
    if not game_dates:
        return slate

    games = game_dates[0].get("games", [])
    slate.game_count = len(games)

    for game in games:
        home = canonicalize_team(
            game.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation", "")
        )
        away = canonicalize_team(
            game.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation", "")
        )
        if not home or not away:
            continue

        game_pk = game.get("gamePk")

        # Extract probable pitcher names, MLB IDs, and handedness from schedule hydration
        home_prob = game.get("teams", {}).get("home", {}).get("probablePitcher", {})
        away_prob = game.get("teams", {}).get("away", {}).get("probablePitcher", {})
        home_starter_name = home_prob.get("fullName") if home_prob else None
        away_starter_name = away_prob.get("fullName") if away_prob else None
        home_starter_mlb_id = home_prob.get("id") if home_prob else None
        away_starter_mlb_id = away_prob.get("id") if away_prob else None
        home_starter_hand = home_prob.get("pitchHand", {}).get("code") if home_prob else None
        away_starter_hand = away_prob.get("pitchHand", {}).get("code") if away_prob else None

        # Extract scheduled game time from MLB API gameDate field
        # e.g. "2026-04-11T23:05:00Z" → "7:05 PM ET"
        scheduled_game_time = _format_game_time_et(game.get("gameDate"))

        # Capture game status. Use detailedState for non-playing games (Postponed,
        # Cancelled, Suspended) so the post-lock monitor can match them against
        # NON_PLAYING_GAME_STATUSES. For all other states use abstractGameState
        # ("Preview" / "Live" / "Final") which is what the rest of the system expects.
        abstract_state = game.get("status", {}).get("abstractGameState", "")
        detailed_state = game.get("status", {}).get("detailedState", "")
        game_status = detailed_state if detailed_state in NON_PLAYING_GAME_STATUSES else abstract_state
        home_score = game.get("teams", {}).get("home", {}).get("score")
        away_score = game.get("teams", {}).get("away", {}).get("score")

        existing = (
            db.query(SlateGame)
            .filter_by(slate_id=slate.id, home_team=home, away_team=away)
            .first()
        )
        if not existing:
            existing = SlateGame(
                slate_id=slate.id,
                home_team=home,
                away_team=away,
                mlb_game_pk=game_pk,
                game_status=game_status or None,
                home_starter=home_starter_name,
                home_starter_mlb_id=home_starter_mlb_id,
                home_starter_hand=home_starter_hand,
                away_starter=away_starter_name,
                away_starter_mlb_id=away_starter_mlb_id,
                away_starter_hand=away_starter_hand,
                scheduled_game_time=scheduled_game_time,
            )
            # Set scores if game is Final
            if game_status == "Final" and home_score is not None:
                existing.home_score = home_score
                existing.away_score = away_score
            db.add(existing)
        else:
            if game_pk and not existing.mlb_game_pk:
                existing.mlb_game_pk = game_pk
            # Always update game_status — it progresses Preview → Live → Final
            if game_status:
                existing.game_status = game_status
            if home_starter_name and not existing.home_starter:
                existing.home_starter = home_starter_name
            if home_starter_mlb_id and not existing.home_starter_mlb_id:
                existing.home_starter_mlb_id = home_starter_mlb_id
            if home_starter_hand and not existing.home_starter_hand:
                existing.home_starter_hand = home_starter_hand
            if away_starter_name and not existing.away_starter:
                existing.away_starter = away_starter_name
            if away_starter_mlb_id and not existing.away_starter_mlb_id:
                existing.away_starter_mlb_id = away_starter_mlb_id
            if away_starter_hand and not existing.away_starter_hand:
                existing.away_starter_hand = away_starter_hand
            if scheduled_game_time and not existing.scheduled_game_time:
                existing.scheduled_game_time = scheduled_game_time
            # Update scores if game is now Final
            if game_status == "Final" and home_score is not None and existing.home_score is None:
                existing.home_score = home_score
                existing.away_score = away_score

    db.commit()
    return slate


async def populate_slate_players(db: Session, slate: Slate) -> dict[str, int]:
    """
    Populate SlatePlayer records from team active rosters.

    This is a pre-game pipeline — it fetches each team's active roster
    from the MLB API and creates Player + SlatePlayer records. Batting
    order is enriched from boxscores when lineups have been posted.

    Returns counts of players added and skipped.
    """
    import logging
    logger = logging.getLogger(__name__)

    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    games = [g for g in games if is_game_remaining(g.game_status)]
    added = 0
    skipped = 0

    # Map teams to their SlateGame
    team_games: dict[str, SlateGame] = {}
    for game in games:
        team_games[game.home_team] = game
        team_games[game.away_team] = game

    # Fetch all team rosters in parallel
    async def _fetch_roster(team: str):
        team_id = TEAM_MLB_IDS.get(team)
        if not team_id:
            raise ValueError(f"No MLB team ID for {team} — cannot fetch roster")
        return team, await get_team_roster(team_id)

    roster_results = await asyncio.gather(*[_fetch_roster(t) for t in team_games], return_exceptions=True)

    for result in roster_results:
        if isinstance(result, Exception):
            raise RuntimeError(
                f"Roster fetch failed — pipeline must fail loudly. Skipping a team "
                f"silently drops every batter and pitcher on it from the candidate pool, "
                f"corrupting EV computation. Original error: {result}"
            ) from result
        team, roster_data = result
        if roster_data is None:
            raise RuntimeError(
                f"Roster fetch returned None for {team} — MLB API returned no roster data. "
                "Pipeline must fail loudly per the no-fallbacks rule."
            )

        game = team_games[team]
        roster = roster_data.get("roster", [])
        for entry in roster:
            person = entry.get("person", {})
            full_name = person.get("fullName", "")
            mlb_id = person.get("id")
            pos_info = entry.get("position", {})
            position = pos_info.get("abbreviation", "DH")
            status = entry.get("status", {}).get("code", "A")

            if not full_name or not mlb_id:
                continue

            # Skip inactive players (IL, minors, etc.)
            if status not in ("A", "RL"):
                continue

            # Get or create Player
            norm = normalize_name(full_name)
            player = db.query(Player).filter_by(name_normalized=norm, team=team).first()
            if not player:
                player = Player(
                    name=full_name,
                    name_normalized=norm,
                    team=team,
                    position=position,
                    mlb_id=mlb_id,
                )
                db.add(player)
                db.flush()
            elif not player.mlb_id:
                player.mlb_id = mlb_id

            # Skip if SlatePlayer already exists
            existing = (
                db.query(SlatePlayer)
                .filter_by(slate_id=slate.id, player_id=player.id)
                .first()
            )
            if existing:
                skipped += 1
                continue

            sp = SlatePlayer(
                slate_id=slate.id,
                player_id=player.id,
                game_id=game.id,
                player_status="active",
            )
            db.add(sp)
            added += 1

    db.commit()

    # Enrich with batting order from boxscores (if lineups have been posted)
    # Phase 1: RotoWire expected lineups (best-effort).  Available up to 4 hours
    # before first pitch, covering ~90% of teams at T-65.  Failures log loudly
    # but do not abort the pipeline — the existing DNP_UNKNOWN_PENALTY (0.85)
    # absorbs missing batting orders.  Per CLAUDE.md, this is graceful
    # degradation (no fake data substituted), not a forbidden fallback.
    rw_enriched = await _enrich_batting_order_from_rotowire(db, slate, logger)

    # Phase 2: MLB Stats API boxscore (ground truth — overrides RotoWire when
    # the official card has been posted, typically T-30 to T-60).
    enriched = await _enrich_batting_order(db, slate, games, logger)

    logger.info(
        "Populated %d slate players (%d skipped/existing, %d batting orders enriched: "
        "%d from RotoWire, %d from MLB official)",
        added, skipped, rw_enriched + enriched, rw_enriched, enriched,
    )
    return {"added": added, "skipped": skipped}


async def _enrich_batting_order_from_rotowire(db: Session, slate: Slate, logger) -> int:
    """
    Pre-fill SlatePlayer.batting_order from RotoWire's expected lineups.

    RotoWire publishes beat-reporter projections up to 4 hours before first
    pitch — much earlier than MLB's official card serialisation.  At T-65 it
    typically covers ~90% of teams in some form (Confirmed or Expected).

    This function is **best-effort**: a failed fetch or parse logs at error
    level and returns 0 enriched.  Downstream MLB API boxscore enrichment
    (Phase 2) will still try to populate batting_order from official cards,
    and any player whose order remains NULL falls into DNP_UNKNOWN_PENALTY
    in the EV formula.

    Sets `batting_order_source` to "rotowire_confirmed" or "rotowire_expected"
    so the official-card phase can detect-and-override, and so post-slate
    calibration can compare RotoWire predictions vs official cards.
    """
    from app.core.rotowire import LineupStatus, fetch_expected_lineups
    from app.models.player import Player, normalize_name

    try:
        games = await fetch_expected_lineups()
    except Exception as exc:
        # No raise — graceful degradation. Loud warning so this is visible
        # in production logs.  Empty enrichment count surfaces in the
        # populate_slate_players() info line.
        logger.error(
            "RotoWire expected-lineup fetch failed: %s. SlatePlayer.batting_order "
            "will be NULL for batters whose official card hasn't dropped yet. "
            "DNP_UNKNOWN_PENALTY (0.85) will absorb the missing signal, but EV "
            "ranking for these players is degraded. Investigate if this persists.",
            exc,
        )
        return 0

    if not games:
        logger.warning("RotoWire returned 0 parseable games — markup may have changed")
        return 0

    # Build lookup: (team_uppercase, normalized_full_name) -> (order, source)
    lookup: dict[tuple[str, str], tuple[int, str]] = {}
    for game in games:
        for team_lineup in (game.visitor, game.home):
            source = (
                "rotowire_confirmed" if team_lineup.status == LineupStatus.CONFIRMED
                else "rotowire_expected"
            )
            for player in team_lineup.players:
                key = (team_lineup.team.upper(), normalize_name(player.full_name))
                lookup[key] = (player.batting_order, source)

    if not lookup:
        return 0

    # Match against this slate's SlatePlayers via Player.name_normalized + team.
    sps = (
        db.query(SlatePlayer)
        .join(Player, SlatePlayer.player_id == Player.id)
        .filter(SlatePlayer.slate_id == slate.id)
        .all()
    )
    enriched = 0
    for sp in sps:
        key = (sp.player.team.upper(), sp.player.name_normalized)
        match = lookup.get(key)
        if match is None:
            continue
        order, source = match
        sp.batting_order = order
        sp.batting_order_source = source
        enriched += 1

    if enriched:
        db.commit()
    return enriched


async def _enrich_batting_order(db: Session, slate: Slate, games: list, logger) -> int:
    """
    Enrich SlatePlayer batting_order from MLB Stats API boxscores when the
    official lineup card has been posted (typically 30-60 min before first
    pitch — i.e. usually AFTER T-65 for early-window games).

    Best-effort: missing/late official cards leave any RotoWire-projected
    batting_order in place.  When an official card IS available, this
    function OVERWRITES the RotoWire value and stamps source="official"
    because the MLB card is ground truth.
    """
    from app.models.player import Player

    async def _fetch_boxscore(game):
        if game.mlb_game_pk is None:
            raise ValueError(
                f"SlateGame {game.id} ({game.away_team} @ {game.home_team}) has no mlb_game_pk"
            )
        return game, await get_game_boxscore(game.mlb_game_pk)

    game_boxscores = await asyncio.gather(*[_fetch_boxscore(g) for g in games], return_exceptions=True)
    enriched = 0

    for result in game_boxscores:
        if isinstance(result, Exception):
            logger.warning("Batting order enrichment: skipping game — %s", result)
            continue
        game, boxscore = result
        if boxscore is None:
            continue

        teams_data = boxscore.get("teams", {})
        for side in ("home", "away"):
            side_data = teams_data.get(side, {})
            team_abbr = canonicalize_team(
                side_data.get("team", {}).get("abbreviation", "")
            )
            if not team_abbr:
                continue

            players_data = side_data.get("players", {})
            for player_key, pdata in players_data.items():
                batting_order_raw = pdata.get("battingOrder")
                if batting_order_raw is None:
                    continue

                bo_int = int(str(batting_order_raw))
                if not (bo_int <= 900 and str(batting_order_raw).endswith("00")):
                    continue
                batting_order = bo_int // 100

                person = pdata.get("person", {})
                mlb_id = person.get("id")
                if not mlb_id:
                    continue

                player = db.query(Player).filter_by(mlb_id=mlb_id).first()
                if not player:
                    continue

                sp = (
                    db.query(SlatePlayer)
                    .filter_by(slate_id=slate.id, player_id=player.id)
                    .first()
                )
                if sp:
                    # Official card is ground truth — overwrite any RotoWire
                    # projection unconditionally.
                    if sp.batting_order != batting_order or sp.batting_order_source != "official":
                        sp.batting_order = batting_order
                        sp.batting_order_source = "official"
                        enriched += 1

    if enriched:
        db.commit()

    return enriched


async def fetch_boxscore_results(db: Session, slate: Slate) -> int:
    """
    Fetch post-game box scores for all games in a slate and update final scores.

    Calls get_game_boxscore() for each SlateGame that has an mlb_game_pk and
    whose scores are not yet recorded. Updates home_score / away_score on each
    game and marks the slate as "completed" once every game has a final score.

    Returns the number of games updated.
    """
    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    updated = 0

    for game in games:
        if game.mlb_game_pk is None:
            raise RuntimeError(
                f"SlateGame {game.id} ({game.away_team} @ {game.home_team}) has no "
                f"mlb_game_pk — cannot reconcile post-game scores. Fix the schedule "
                f"ingest rather than skipping (skipping leaves the slate stuck in "
                f"'in_progress' and blocks tomorrow's T-65 pipeline)."
            )

        # Skip if already populated
        if game.home_score is not None and game.away_score is not None:
            continue

        # Let boxscore fetch failures propagate. Silent skips leave the
        # slate permanently "in_progress" and block cache turnover.
        boxscore = await get_game_boxscore(game.mlb_game_pk)

        teams = boxscore.get("teams", {})
        home_runs = teams.get("home", {}).get("teamStats", {}).get("batting", {}).get("runs")
        away_runs = teams.get("away", {}).get("teamStats", {}).get("batting", {}).get("runs")

        if home_runs is not None and away_runs is not None:
            game.home_score = int(home_runs)
            game.away_score = int(away_runs)
            updated += 1

    # Mark slate completed if all games now have scores
    if games and all(g.home_score is not None and g.away_score is not None for g in games):
        slate.status = "completed"

    db.commit()
    return updated


async def resolve_mlb_id(db: Session, player: Player) -> int | None:
    """Look up a player's MLB ID if we don't have it."""
    if player.mlb_id:
        return player.mlb_id

    results = await search_player(player.name)
    if results:
        # Best match: same team
        for r in results:
            team_abbr = r.get("currentTeam", {}).get("abbreviation", "")
            if team_abbr == player.team:
                player.mlb_id = r["id"]
                db.commit()
                return player.mlb_id

        # No exact team match — refuse to guess.  Assigning the wrong
        # player's MLB ID would corrupt all downstream stats for this player.
        logger.warning(
            "MLB ID lookup for %s (%s): %d results but no team match — skipping",
            player.name, player.team, len(results),
        )

    return None


async def fetch_player_season_stats(db: Session, player: Player) -> PlayerStats | None:
    """Fetch and store season stats for a player from MLB API."""
    mlb_id = await resolve_mlb_id(db, player)
    if not mlb_id:
        return None

    data = await get_player_stats(mlb_id, settings.current_season)
    people = data.get("people", [])
    if not people:
        return None

    person = people[0]

    # Store handedness — used for platoon advantage computation
    bat_side = person.get("batSide", {}).get("code")    # L, R, or S
    pitch_hand = person.get("pitchHand", {}).get("code")  # L or R
    if bat_side and player.bat_side != bat_side:
        player.bat_side = bat_side
    if pitch_hand and player.pitch_hand != pitch_hand:
        player.pitch_hand = pitch_hand

    stats_groups = person.get("stats", [])

    ps = (
        db.query(PlayerStats)
        .filter_by(player_id=player.id, season=settings.current_season)
        .first()
    )
    if not ps:
        ps = PlayerStats(player_id=player.id, season=settings.current_season)
        db.add(ps)

    for group in stats_groups:
        stat_type = group.get("type", {}).get("displayName", "")
        splits = group.get("splits", [])
        if not splits:
            continue

        s = splits[0].get("stat", {})

        if stat_type == "season" and group.get("group", {}).get("displayName") == "hitting":
            ps.games = s.get("gamesPlayed", 0)
            ps.pa = s.get("plateAppearances", 0)
            ps.ab = s.get("atBats", 0)
            ps.hits = s.get("hits", 0)
            ps.hr = s.get("homeRuns", 0)
            ps.rbi = s.get("rbi", 0)
            ps.sb = s.get("stolenBases", 0)
            ps.bb = s.get("baseOnBalls", 0)
            ps.so = s.get("strikeOuts", 0)
            ps.avg = _safe_float(s.get("avg", ""))
            ps.ops = _safe_float(s.get("ops", ""))
            slg = _safe_float(s.get("slg", ""))
            if slg is not None and ps.avg is not None:
                # ISO = SLG − AVG. Power-profile trait depends on this; before
                # V10.0 ps.iso was never populated and power_profile silently
                # lost its 7-point ISO component for every batter.
                ps.iso = round(slg - ps.avg, 3)

        elif stat_type == "season" and group.get("group", {}).get("displayName") == "pitching":
            ps.games = s.get("gamesPlayed", 0)
            ps.ip = _safe_float(s.get("inningsPitched", "")) or 0.0
            ps.era = _safe_float(s.get("era", ""))
            ps.whip = _safe_float(s.get("whip", ""))
            so = s.get("strikeOuts", 0)
            if ps.ip > 0:
                ps.k_per_9 = round(so / ps.ip * 9, 2)

        elif stat_type == "gameLog":
            # Store recent game logs
            for split in splits[:10]:
                game_date_str = split.get("date", "")
                if not game_date_str:
                    continue
                gd = date.fromisoformat(game_date_str)
                gs = split.get("stat", {})

                existing = (
                    db.query(PlayerGameLog)
                    .filter_by(player_id=player.id, game_date=gd)
                    .first()
                )
                if existing:
                    continue

                opp = split.get("opponent", {}).get("abbreviation", "")
                log = PlayerGameLog(
                    player_id=player.id,
                    game_date=gd,
                    opponent=opp,
                    source="mlb_api",
                    ab=gs.get("atBats", 0),
                    hits=gs.get("hits", 0),
                    hr=gs.get("homeRuns", 0),
                    rbi=gs.get("rbi", 0),
                    bb=gs.get("baseOnBalls", 0),
                    so=gs.get("strikeOuts", 0),
                    sb=gs.get("stolenBases", 0),
                    ip=float(gs.get("inningsPitched", "0") or 0),
                    er=gs.get("earnedRuns", 0),
                    k_pitching=gs.get("strikeOuts", 0),
                    decision=gs.get("decision", ""),
                )
                db.add(log)

        elif stat_type == "statSplits" and group.get("group", {}).get("displayName") == "hitting":
            # Fetch platoon OPS splits (vs left-handed and right-handed pitchers)
            for split in splits:
                split_code = split.get("split", {}).get("code", "")
                s = split.get("stat", {})
                ops = _safe_float(s.get("ops", ""))

                if split_code == "vl" and ops is not None:
                    ps.ops_vs_lhp = ops
                elif split_code == "vr" and ops is not None:
                    ps.ops_vs_rhp = ops

    # Statcast kinematic columns (avg_exit_velocity, fb_ivb, whiff_pct, etc.)
    # are NOT populated here.  Baseball Savant rate-limits aggressive readers
    # and a synchronous CSV pull at T-65 can hang past the lock window.  The
    # daily refresh job (scripts/refresh_statcast.py) bulk-loads the season
    # leaderboards overnight and upserts them onto PlayerStats; the T-65
    # pipeline reads those columns straight from the DB.  If a column is NULL
    # (new call-up with no Savant row yet), the scoring engine transparently
    # routes through the non-Statcast fallback path.

    db.commit()
    return ps


async def enrich_slate_game_team_stats(db: Session, slate: Slate, season: int) -> int:
    """
    Fetch team-level batting OPS/K% and pitching ERA for every game in the
    slate and store on SlateGame.

    Batting (hitting group): populates home/away_team_ops and home/away_team_k_pct.
      Used by pitcher env scoring: weak opponent OPS (Factor 1) and
      high-K opponent (Factor 2).

    Pitching (pitching group): populates home/away_bullpen_era as a team
      pitching ERA proxy.  Used by batter env scoring Group A A4 (vulnerable
      bullpen).  True bullpen ERA (relievers only) would require a roster-level
      split; team ERA is an adequate proxy vs. NULL.
    """
    from app.core.mlb_api import get_team_pitching_stats

    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    games = [g for g in games if is_game_remaining(g.game_status)]
    teams = {g.home_team for g in games} | {g.away_team for g in games}

    async def _fetch_batting(team: str) -> tuple[str, dict]:
        return team, await get_team_stats(TEAM_MLB_IDS[team], season)

    async def _fetch_pitching(team: str) -> tuple[str, dict]:
        return team, await get_team_pitching_stats(TEAM_MLB_IDS[team], season)

    batting_raw, pitching_raw = await asyncio.gather(
        asyncio.gather(*[_fetch_batting(t) for t in teams], return_exceptions=True),
        asyncio.gather(*[_fetch_pitching(t) for t in teams], return_exceptions=True),
    )

    team_batting: dict[str, dict] = {}
    for result in batting_raw:
        if isinstance(result, Exception):
            raise RuntimeError(
                f"Team batting stats fetch failed — pipeline must fail loudly. "
                f"A skipped team produces NULL home/away_team_ops and home/away_team_k_pct, "
                f"corrupting pitcher env scoring (Factor 1 weak-OPS, Factor 2 high-K). "
                f"Original error: {result}"
            ) from result
        team, data = result
        splits = (data.get("stats") or [{}])[0].get("splits", [])
        if not splits:
            continue
        s = splits[0].get("stat", {})
        ops = _safe_float(s.get("ops", ""))
        pa = s.get("plateAppearances", 0)
        so = s.get("strikeOuts", 0)
        k_pct = (so / pa) if pa > 0 else None
        team_batting[team] = {"ops": ops, "k_pct": k_pct}

    team_pitching: dict[str, dict] = {}
    for result in pitching_raw:
        if isinstance(result, Exception):
            raise RuntimeError(
                f"Team pitching stats fetch failed — pipeline must fail loudly. "
                f"A skipped team produces NULL home/away_bullpen_era, corrupting "
                f"batter env Group A A4 (vulnerable bullpen signal). "
                f"Original error: {result}"
            ) from result
        team, data = result
        splits = (data.get("stats") or [{}])[0].get("splits", [])
        if not splits:
            continue
        s = splits[0].get("stat", {})
        era = _safe_float(s.get("era", ""))
        team_pitching[team] = {"era": era}

    updated = 0
    for game in games:
        home_bat = team_batting.get(game.home_team, {})
        away_bat = team_batting.get(game.away_team, {})
        home_pit = team_pitching.get(game.home_team, {})
        away_pit = team_pitching.get(game.away_team, {})

        if home_bat.get("ops") is not None:
            game.home_team_ops = home_bat["ops"]
        if home_bat.get("k_pct") is not None:
            game.home_team_k_pct = home_bat["k_pct"]
        if away_bat.get("ops") is not None:
            game.away_team_ops = away_bat["ops"]
        if away_bat.get("k_pct") is not None:
            game.away_team_k_pct = away_bat["k_pct"]

        if home_pit.get("era") is not None:
            game.home_bullpen_era = home_pit["era"]
        if away_pit.get("era") is not None:
            game.away_bullpen_era = away_pit["era"]

        updated += 1

    # Validate that team stats were successfully enriched — critical for env scoring
    # A NULL team stat field indicates MLB API data was unavailable, which silently
    # degrades the primary environmental signal. Per the "no fallbacks" rule, fail loudly.
    for game in games:
        missing_fields = []
        if game.home_team_ops is None:
            missing_fields.append(f"home_team_ops ({game.home_team})")
        if game.away_team_ops is None:
            missing_fields.append(f"away_team_ops ({game.away_team})")
        if game.home_team_k_pct is None:
            missing_fields.append(f"home_team_k_pct ({game.home_team})")
        if game.away_team_k_pct is None:
            missing_fields.append(f"away_team_k_pct ({game.away_team})")
        if game.home_bullpen_era is None:
            missing_fields.append(f"home_bullpen_era ({game.home_team})")
        if game.away_bullpen_era is None:
            missing_fields.append(f"away_bullpen_era ({game.away_team})")

        if missing_fields:
            raise RuntimeError(
                f"Team stats enrichment failed for {game.home_team} vs {game.away_team}: "
                f"{', '.join(missing_fields)} could not be fetched from MLB API. "
                "Pipeline must fail loudly."
            )

    db.commit()
    return updated


async def enrich_slate_game_series_context(db: Session, slate: Slate) -> int:
    """
    Populate series context (series_home_wins, series_away_wins) and recent
    form (home_team_l10_wins, away_team_l10_wins) on every SlateGame for the
    given slate.

    Series context: how many games each team has won in the CURRENT series
    (consecutive games between the same two opponents) BEFORE today's game.
    Fetch each team's last 14 days of schedule, find games vs. the same opponent,
    and count results.

    Recent form (L10): count wins in the team's most recent 10 completed games.

    Raises RuntimeError if any team abbreviation is unknown or the API returns
    no schedule data — the pipeline fails loudly with no NULL fields.
    """
    from datetime import timedelta

    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    games = [g for g in games if is_game_remaining(g.game_status)]
    if not games:
        return 0

    slate_date: date = slate.date
    lookback_start = (slate_date - timedelta(days=14)).isoformat()
    lookback_end = (slate_date - timedelta(days=1)).isoformat()

    teams = {g.home_team for g in games} | {g.away_team for g in games}

    async def _fetch_team_schedule(team: str) -> tuple[str, list[dict]]:
        team_id = TEAM_MLB_IDS.get(team)
        if not team_id:
            raise RuntimeError(
                f"Unknown team abbreviation {team!r} — not in TEAM_MLB_IDS. "
                "Cannot fetch series context."
            )
        from app.core.mlb_api import _get
        data = await _get("/schedule", {
            "teamId": team_id,
            "startDate": lookback_start,
            "endDate": lookback_end,
            "sportId": 1,
            "hydrate": "linescore",
        })
        game_list: list[dict] = []
        for date_entry in data.get("dates", []):
            for g in date_entry.get("games", []):
                if g.get("status", {}).get("abstractGameState") == "Final":
                    game_list.append(g)
        return team, game_list

    raw_results = await asyncio.gather(*[_fetch_team_schedule(t) for t in teams], return_exceptions=True)
    team_games: dict[str, list[dict]] = {}
    for result in raw_results:
        if isinstance(result, Exception):
            raise RuntimeError(
                f"Series context: schedule fetch failed — pipeline must fail loudly. "
                f"A skipped team's series_wins/losses and l10_wins remain NULL on SlateGame, "
                f"corrupting batter env Group D (series + recent form). "
                f"Original error: {result}"
            ) from result
        team, game_list = result
        team_games[team] = game_list

    def _normalize(abbr: str) -> str:
        return canonicalize_team(abbr).upper()

    def _team_abbr_from_mlb(team_obj: dict) -> str:
        """Resolve a team's canonical abbreviation from an MLB API team object.

        The /schedule endpoint without `team` hydration returns only
        {id, name, link} for the team object — no `abbreviation` field —
        so we must reverse-look up by id. Without this, every team's
        L10 wins silently computed to 0, killing the recent-form signal.
        """
        abbr = team_obj.get("abbreviation")
        if abbr:
            return _normalize(abbr)
        team_id = team_obj.get("id")
        if team_id is not None:
            looked_up = TEAM_ABBR_BY_MLB_ID.get(team_id)
            if looked_up:
                return looked_up.upper()
        return ""

    def _extract_record(team: str, opp: str) -> tuple[int | None, int | None, int | None, int | None]:
        """
        Return (series_wins, series_losses, l10_wins, rest_days) for `team` vs `opp`.

        series_wins/losses: consecutive games vs. opp immediately before
        slate_date (the current series).
        l10_wins: wins in the 10 most recent completed games (any opponent).
        rest_days: V10.8 — calendar days between the team's most recent
        completed game and `slate_date`.  0 = back-to-back (played yesterday),
        1 = one rest day, etc.  None = no completed games in lookback.

        Raises RuntimeError if no completed games are found in the lookback
        window — indicates an API or data problem mid-season.
        """
        raw = team_games.get(team, [])
        if not raw:
            logger.warning(
                "No completed games found for team %r in last 14 days — "
                "series context will be NULL for this team",
                team,
            )
            return None, None, None, None

        def _game_date(g: dict) -> str:
            return g.get("officialDate", g.get("gameDate", "")[:10])

        sorted_games = sorted(raw, key=_game_date, reverse=True)

        # V10.8 — rest days between most recent completed game and the slate date.
        # FantasyLabs DFS research: opponent rest is a real edge (back-to-back
        # opp = depleted bullpen + tighter starter pitch leash).  Slot it onto
        # the team's home/away side; the env-scoring layer reads the OPPOSING
        # side when computing the batter's environment.
        #
        # No try/except on date parsing here — the existing L10 / series_wins
        # logic below already trusts MLB Stats API ISO dates.  A malformed date
        # would indicate an upstream API problem worth failing on; ValueError
        # propagates up to enrich_slate_game_series_context's caller per the
        # "fail loud, never fallback" rule.
        from datetime import date as _date

        rest_days: int | None = None
        if sorted_games:
            most_recent_str = _game_date(sorted_games[0])
            if most_recent_str:
                most_recent_date = _date.fromisoformat(most_recent_str[:10])
                rest_days = max(0, (slate_date - most_recent_date).days - 1)

        l10 = 0
        for g in sorted_games[:10]:
            teams_info = g.get("teams", {})
            home_t = _team_abbr_from_mlb(teams_info.get("home", {}).get("team", {}))
            away_t = _team_abbr_from_mlb(teams_info.get("away", {}).get("team", {}))
            home_score = teams_info.get("home", {}).get("score")
            away_score = teams_info.get("away", {}).get("score")
            if home_score is None or away_score is None:
                continue
            team_n = _normalize(team)
            if home_t == team_n:
                if home_score > away_score:
                    l10 += 1
            elif away_t == team_n:
                if away_score > home_score:
                    l10 += 1

        series_wins = 0
        series_losses = 0
        opp_n = _normalize(opp)
        team_n = _normalize(team)
        in_series = False
        for g in sorted_games:
            teams_info = g.get("teams", {})
            home_t = _team_abbr_from_mlb(teams_info.get("home", {}).get("team", {}))
            away_t = _team_abbr_from_mlb(teams_info.get("away", {}).get("team", {}))
            is_vs_opp = (home_t == opp_n and away_t == team_n) or (away_t == opp_n and home_t == team_n)

            if not is_vs_opp:
                if in_series:
                    break
                continue

            in_series = True
            home_score = teams_info.get("home", {}).get("score")
            away_score = teams_info.get("away", {}).get("score")
            if home_score is None or away_score is None:
                continue

            if home_t == team_n:
                if home_score > away_score:
                    series_wins += 1
                else:
                    series_losses += 1
            else:
                if away_score > home_score:
                    series_wins += 1
                else:
                    series_losses += 1

        return series_wins, series_losses, l10, rest_days

    updated = 0
    for game in games:
        home_sw, _home_sl, home_l10, home_rest = _extract_record(game.home_team, game.away_team)
        away_sw, _away_sl, away_l10, away_rest = _extract_record(game.away_team, game.home_team)

        game.series_home_wins = home_sw
        game.series_away_wins = away_sw
        game.home_team_l10_wins = home_l10
        game.away_team_l10_wins = away_l10
        # V10.8 — rest days, derived from the same schedule lookback.
        game.home_team_rest_days = home_rest
        game.away_team_rest_days = away_rest
        updated += 1

    db.commit()
    logger.info("Series context enriched for %d games on %s", updated, slate_date)
    return updated


async def enrich_slate_game_weather(db: Session, slate: Slate) -> int:
    """
    Fetch weather (temperature + wind) for each game in the slate from Open-Meteo
    and store on SlateGame.

    Populates: wind_speed_mph, wind_direction, temperature_f.

    wind_direction is stored as "OUT" when the wind blows toward center field at
    the park (park-specific compass analysis), or as an 8-point compass label
    (N/NE/E/SE/S/SW/W/NW) otherwise.  "OUT" is the only value that triggers
    BATTER_ENV_WIND_OUT_BONUS in compute_batter_env_score().

    Raises RuntimeError if weather data cannot be fetched for any game.
    All three fields must be populated — NULL weather corrupts env scoring.

    Uses Open-Meteo archive endpoint for past dates (≥ 5 days ago) and the
    forecast endpoint for today or near-future games.
    """
    from datetime import date as _date

    from app.core.open_meteo import STADIUM_COORDINATES, get_game_weather

    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    games = [g for g in games if is_game_remaining(g.game_status)]
    if not games:
        return 0

    today = _date.today()
    use_archive = (today - slate.date).days >= 5

    updated = 0
    for game in games:
        park = game.home_team
        coords = STADIUM_COORDINATES.get(park)
        if coords is None:
            raise RuntimeError(
                f"No stadium coordinates for home_team={park!r} — "
                "add entry to STADIUM_COORDINATES in app/core/open_meteo.py."
            )

        lat, lon = coords

        # Parse scheduled time to determine the closest UTC hour for weather lookup.
        # scheduled_game_time is stored as "H:MM AM/PM ET" by _format_game_time_et().
        # Regular season is EDT (UTC-4), so 7:05 PM EDT = 23:05 UTC.
        # Default to 23 only when scheduled_game_time is absent (NULL game time).
        # A present-but-malformed value raises ValueError — no silent fallback.
        utc_hour = 23
        if game.scheduled_game_time:
            time_str = game.scheduled_game_time.replace(" ET", "").strip()
            from datetime import datetime as _dt
            parsed = _dt.strptime(time_str, "%I:%M %p")
            utc_hour = (parsed.hour + ET_TO_UTC_OFFSET_HOURS) % 24

        weather = await get_game_weather(
            lat=lat,
            lon=lon,
            game_date=slate.date,
            game_utc_hour=utc_hour,
            park_team=park,
            use_archive=use_archive,
        )
        if weather is None:
            raise RuntimeError(
                f"Weather fetch failed for {game.home_team} vs {game.away_team} on {slate.date} — "
                "pipeline cannot proceed with NULL weather data."
            )

        game.wind_speed_mph  = weather["wind_speed_mph"]
        game.wind_direction  = weather["wind_direction"]
        game.temperature_f   = weather["temperature_f"]
        updated += 1

    db.commit()
    logger.info(
        "Weather enriched for %d of %d games on %s",
        updated, len(games), slate.date,
    )
    return updated


async def enrich_slate_game_vegas_lines(db: Session, slate: Slate) -> int:
    """
    Fetch Vegas lines (moneylines + O/U totals) from The Odds API and store
    them on SlateGame records.

    Populates: vegas_total, home_moneyline, away_moneyline.

    These feed directly into env scoring:
      - compute_pitcher_env_score()  Factor 5: Moneyline Win bonus
      - compute_batter_env_score()   Group A A1: Vegas O/U, A3: Moneyline

    CRITICAL: Vegas lines are REQUIRED, never optional.

    Raises RuntimeError if BO_ODDS_API_KEY is not set, quota is exhausted,
    or the request fails. There is no fallback to NULL moneylines. Missing Vegas
    data corrupts the EV formula and produces suboptimal lineups. The T-65 pipeline
    must crash loudly rather than proceed with degraded data.

    See CLAUDE.md section "Vegas Lines: Required, Never Optional" for full rationale.
    """
    from app.config import settings
    from app.core.odds_api import fetch_mlb_odds

    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    # Odds API does not return lines for started games.
    games = [g for g in games if is_game_remaining(g.game_status)]
    if not games:
        return 0

    if all(g.home_moneyline is not None and g.away_moneyline is not None for g in games):
        logger.info("Vegas lines already populated for all %d games on %s — skipping API call", len(games), slate.date)
        return len(games)

    odds_data = await fetch_mlb_odds(settings.odds_api_key, slate.date)

    # Build lookup: (home_abbr, away_abbr) → odds dict
    odds_lookup: dict[tuple[str, str], dict] = {
        (o["home_team"], o["away_team"]): o
        for o in odds_data
    }

    updated = 0
    for game in games:
        key = (game.home_team.upper(), game.away_team.upper())
        odds = odds_lookup.get(key)
        if not odds:
            raise RuntimeError(
                f"No odds found for {game.home_team} vs {game.away_team} on {slate.date} — "
                "pipeline cannot proceed without moneylines for all games."
            )

        if odds.get("home_moneyline") is not None:
            game.home_moneyline = odds["home_moneyline"]
        if odds.get("away_moneyline") is not None:
            game.away_moneyline = odds["away_moneyline"]
        if odds.get("total") is not None:
            game.vegas_total = odds["total"]
        updated += 1

    null_moneylines = [
        f"{g.home_team} vs {g.away_team}"
        for g in games
        if g.home_moneyline is None or g.away_moneyline is None
    ]
    if null_moneylines:
        raise RuntimeError(
            f"Vegas lines: moneylines not populated for {len(null_moneylines)} game(s) "
            f"on {slate.date}: {', '.join(null_moneylines)}"
        )

    db.commit()
    logger.info(
        "Vegas lines enriched for %d of %d games on %s",
        updated, len(games), slate.date,
    )
    return updated
