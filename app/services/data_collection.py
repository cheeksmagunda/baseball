"""
Data collection service: fetches player stats from MLB Stats API
and stores them in the database.
"""

import asyncio
from datetime import date, datetime as _datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import settings
from app.core.constants import canonicalize_team
from app.core.mlb_api import (
    get_schedule,
    get_game_boxscore,
    get_player_stats,
    get_team_stats,
    get_team_roster,
    search_player,
    TEAM_MLB_IDS,
)
from app.models.player import Player, PlayerStats, PlayerGameLog, normalize_name
from app.models.slate import Slate, SlateGame, SlatePlayer

_ET = ZoneInfo("America/New_York")


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
    try:
        utc_dt = _datetime.fromisoformat(game_date_iso.replace("Z", "+00:00"))
        et_dt = utc_dt.astimezone(_ET)
        formatted = et_dt.strftime("%I:%M %p")
        # Strip leading zero: "07:05 PM" → "7:05 PM"
        if formatted.startswith("0"):
            formatted = formatted[1:]
        return f"{formatted} ET"
    except (ValueError, TypeError):
        return None


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

        # Extract probable pitcher names and MLB IDs from schedule hydration
        home_prob = game.get("teams", {}).get("home", {}).get("probablePitcher", {})
        away_prob = game.get("teams", {}).get("away", {}).get("probablePitcher", {})
        home_starter_name = home_prob.get("fullName") if home_prob else None
        away_starter_name = away_prob.get("fullName") if away_prob else None
        home_starter_mlb_id = home_prob.get("id") if home_prob else None
        away_starter_mlb_id = away_prob.get("id") if away_prob else None

        # Extract scheduled game time from MLB API gameDate field
        # e.g. "2026-04-11T23:05:00Z" → "7:05 PM ET"
        scheduled_game_time = _format_game_time_et(game.get("gameDate"))

        # Capture scores from schedule response for completed games
        game_status = game.get("status", {}).get("abstractGameState", "")
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
                away_starter=away_starter_name,
                away_starter_mlb_id=away_starter_mlb_id,
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
            if away_starter_name and not existing.away_starter:
                existing.away_starter = away_starter_name
            if away_starter_mlb_id and not existing.away_starter_mlb_id:
                existing.away_starter_mlb_id = away_starter_mlb_id
            if scheduled_game_time and not existing.scheduled_game_time:
                existing.scheduled_game_time = scheduled_game_time
            # Update scores if game is now Final
            if game_status == "Final" and home_score is not None and existing.home_score is None:
                existing.home_score = home_score
                existing.away_score = away_score

    db.commit()
    return slate


async def populate_slate_players(db: Session, slate: Slate) -> dict:
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
            logger.warning("No MLB team ID for %s — skipping roster fetch", team)
            return team, None
        try:
            return team, await get_team_roster(team_id)
        except Exception as exc:
            logger.warning("Failed to fetch roster for %s: %s", team, exc)
            return team, None

    roster_results = await asyncio.gather(*[_fetch_roster(t) for t in team_games])

    for team, roster_data in roster_results:
        if roster_data is None:
            continue

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
                card_boost=0.0,
                game_id=game.id,
                player_status="active",
            )
            db.add(sp)
            added += 1

    db.commit()

    # Enrich with batting order from boxscores (if lineups have been posted)
    enriched = await _enrich_batting_order(db, slate, games, logger)

    logger.info("Populated %d slate players (%d skipped/existing, %d batting orders enriched)", added, skipped, enriched)
    return {"added": added, "skipped": skipped}


async def _enrich_batting_order(db: Session, slate: Slate, games: list, logger) -> int:
    """
    Enrich SlatePlayer batting_order from boxscores when lineups are posted.

    This is optional enrichment — the pipeline works without it. When lineups
    are available (typically 30-60 min before first pitch), boxscores contain
    batting order data that feeds the lineup_position scoring trait.
    """
    async def _fetch_boxscore(game):
        if game.mlb_game_pk is None:
            return game, None
        try:
            return game, await get_game_boxscore(game.mlb_game_pk)
        except Exception:
            return game, None

    game_boxscores = await asyncio.gather(*[_fetch_boxscore(g) for g in games])
    enriched = 0

    for game, boxscore in game_boxscores:
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
                if sp and sp.batting_order is None:
                    sp.batting_order = batting_order
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
    import logging
    logger = logging.getLogger(__name__)

    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    updated = 0

    for game in games:
        if game.mlb_game_pk is None:
            logger.warning("SlateGame %s (%s @ %s) has no mlb_game_pk — skipping", game.id, game.away_team, game.home_team)
            continue

        # Skip if already populated
        if game.home_score is not None and game.away_score is not None:
            continue

        try:
            boxscore = await get_game_boxscore(game.mlb_game_pk)
        except Exception as exc:
            logger.warning("Failed to fetch boxscore for game_pk=%s: %s", game.mlb_game_pk, exc)
            continue

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

        # Fallback: first result
        player.mlb_id = results[0]["id"]
        db.commit()
        return player.mlb_id

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
            avg_str = s.get("avg", "")
            ps.avg = float(avg_str) if avg_str else None
            ops_str = s.get("ops", "")
            ps.ops = float(ops_str) if ops_str else None

        elif stat_type == "season" and group.get("group", {}).get("displayName") == "pitching":
            ps.games = s.get("gamesPlayed", 0)
            ip_str = s.get("inningsPitched", "0")
            ps.ip = float(ip_str) if ip_str else 0.0
            era_str = s.get("era", "")
            ps.era = float(era_str) if era_str else None
            whip_str = s.get("whip", "")
            ps.whip = float(whip_str) if whip_str else None
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

    db.commit()
    return ps


async def enrich_slate_game_team_stats(db: Session, slate: Slate, season: int) -> int:
    """
    Fetch team-level batting OPS and K% for every game in the slate and store on SlateGame.

    Used by Filter 2 pitcher env scoring: weak opponent OPS (Factor 1) and
    high-K opponent (Factor 2). Both were previously always None because no
    code populated the team-level stats.
    """
    import logging
    logger = logging.getLogger(__name__)

    games = db.query(SlateGame).filter_by(slate_id=slate.id).all()
    teams = {g.home_team for g in games} | {g.away_team for g in games}

    async def _fetch(team: str) -> tuple[str, dict | None]:
        team_id = TEAM_MLB_IDS.get(team)
        if not team_id:
            return team, None
        try:
            return team, await get_team_stats(team_id, season)
        except Exception as exc:
            logger.warning("Team stats fetch failed for %s: %s", team, exc)
            return team, None

    results = await asyncio.gather(*[_fetch(t) for t in teams])

    team_stats: dict[str, dict] = {}
    for team, data in results:
        if data is None:
            continue
        splits = (data.get("stats") or [{}])[0].get("splits", [])
        if not splits:
            continue
        s = splits[0].get("stat", {})
        ops_str = s.get("ops", "")
        ops = float(ops_str) if ops_str else None
        pa = s.get("plateAppearances", 0)
        so = s.get("strikeOuts", 0)
        k_pct = (so / pa) if pa > 0 else None
        team_stats[team] = {"ops": ops, "k_pct": k_pct}

    updated = 0
    for game in games:
        home = team_stats.get(game.home_team, {})
        away = team_stats.get(game.away_team, {})
        if home.get("ops") is not None:
            game.home_team_ops = home["ops"]
        if home.get("k_pct") is not None:
            game.home_team_k_pct = home["k_pct"]
        if away.get("ops") is not None:
            game.away_team_ops = away["ops"]
        if away.get("k_pct") is not None:
            game.away_team_k_pct = away["k_pct"]
        updated += 1

    db.commit()
    return updated
