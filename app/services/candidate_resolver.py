"""Candidate resolver — converts FilterCards + game environments into
FilteredCandidates ready for the optimizer.

Stages:
  1. Stage 0: map each card's (player_name, team) to a Player row in one
     batched SQL query (see find_players_by_name_team_batch).
  2. Stage 1: synchronous work — score traits, compute env score, detect
     two-way pitchers, capture series context.
  3. Stage 2: assemble FilteredCandidate instances + emit health-check logs.

V15: each candidate is annotated with a predicted popularity score from
app/core/popularity.py (continuous float in [0, 10]).  The score drives
leverage_factor in _compute_base_ev so the optimizer prefers, all else
equal, the player the field is less likely to draft.  See
STRATEGY_AUDIT_2026-05.md for the empirical case.

Moved out of app/routers/filter_strategy.py so the router stays thin and
this logic is independently testable.
"""

import logging
from datetime import date

from sqlalchemy.orm import Session

from app.config import settings
from app.core.constants import (
    PITCHER_POSITIONS,
)
from app.core.popularity import predict_popularity_score, predict_rookie_popularity_score
from app.core.utils import find_players_by_name_team_batch
from app.models.player import PlayerStats, TeamSeasonStats
from app.schemas.filter_strategy import FilterCard, GameEnvironment
from app.services.filter_strategy import (
    FilteredCandidate,
    build_batter_env_kwargs,
    build_pitcher_env_kwargs,
    compute_batter_env_score,
    compute_pitcher_env_score,
)
from app.services.scoring_engine import score_player

logger = logging.getLogger(__name__)


def _tokens(s: str) -> frozenset[str]:
    """Token-set helper for fuzzy name matching (strips dots, lowercases)."""
    return frozenset(t for t in s.lower().replace(".", " ").split() if len(t) >= 2)


def _build_game_lookup(games: list[GameEnvironment]) -> tuple[dict, dict]:
    """Build lookup dicts from game environment data."""
    game_by_id: dict = {}
    team_to_game: dict = {}
    for g in games:
        if g.game_id is not None:
            game_by_id[g.game_id] = g
        team_to_game[g.home_team.upper()] = g
        team_to_game[g.away_team.upper()] = g
    return game_by_id, team_to_game


def _detect_two_way_pitcher(player, card: FilterCard, game: GameEnvironment) -> bool:
    """Check if a non-pitcher (e.g., DH) is the confirmed starter.

    Returns True if detected as a confirmed starter, False otherwise.

    Matching priority:
      1. mlb_id equality — the only authoritative match.
      2. Token-set equality on lowercased, dot-stripped names — prevents
         "Smith" from matching "Smith Jr." (false positive in the previous
         loose substring check).  Both names must share the same set of
         length-≥2 tokens to count as a match.
    """
    is_home = game.home_team.upper() == card.team.upper()
    starter_mlb_id = game.home_starter_mlb_id if is_home else game.away_starter_mlb_id
    starter_name = game.home_starter if is_home else game.away_starter

    if starter_mlb_id is not None and player.mlb_id == starter_mlb_id:
        logger.info(
            "Two-way player detected: %s (%s) is confirmed starter — treating as SP",
            card.player_name, card.team,
        )
        return True

    if starter_name is not None and player.mlb_id is None:
        # Only fall back to name matching if we have no mlb_id to verify
        # against — otherwise mlb_id mismatch above is the real signal.
        card_tokens = _tokens(card.player_name)
        prob_tokens = _tokens(starter_name)
        if card_tokens and card_tokens == prob_tokens:
            logger.info(
                "Two-way player detected (token-set match): %s (%s) is confirmed starter — treating as SP",
                card.player_name, card.team,
            )
            return True

    return False




async def resolve_candidates(
    cards: list[FilterCard],
    games: list[GameEnvironment],
    db: Session,
    slate_date: date | None = None,
) -> list[FilteredCandidate]:
    """Resolve cards into FilteredCandidates ready for the optimizer.

    See module docstring for the 3-stage flow.

    Args:
        slate_date: The date of the slate being resolved.  Required for
            the rolling 14-day fame index in popularity prediction.
            Defaults to today if not supplied (production callers always
            pass the active slate date so the prediction is point-in-time
            correct).
    """
    game_by_id, team_to_game = _build_game_lookup(games)

    # Drop cards without a player_name. Draft counts are ingested post-slate,
    # so drafts=None is routine pre-game and should NOT filter the card out.
    cards = [c for c in cards if c.player_name]

    _current_season = settings.current_season
    as_of = slate_date or date.today()

    # Pre-build a team framing-runs lookup for the slate.  One SQL
    # query per slate, keyed by team abbreviation.  Pass into score_player so
    # `score_pitcher_k_rate` can apply the small ±5% framing adjustment.
    # Falls through cleanly when TeamSeasonStats has no row for a team
    # (refresh hasn't run yet, or Savant scrape failed).
    teams_in_slate = {g.home_team.upper() for g in games} | {g.away_team.upper() for g in games}
    team_framing_lookup: dict[str, float] = {}
    if teams_in_slate:
        framing_rows = (
            db.query(TeamSeasonStats)
            .filter(
                TeamSeasonStats.team.in_(teams_in_slate),
                TeamSeasonStats.season == _current_season,
            )
            .all()
        )
        for row in framing_rows:
            if row.framing_runs is not None:
                team_framing_lookup[row.team.upper()] = row.framing_runs

    # Stage 0: map cards to Player records in one batched query.
    pairs = [(c.player_name, c.team) for c in cards]
    player_map = find_players_by_name_team_batch(db, pairs)
    card_player_map: dict = {}
    for card in cards:
        player = player_map.get((card.player_name, card.team))
        if not player:
            raise ValueError(
                f"Player {card.player_name!r} ({card.team}) not found in database — "
                "pipeline data integrity error"
            )
        card_player_map[f"{card.player_name}|{card.team}"] = player

    # Stage 0b: batch-load PlayerStats for popularity prediction (OPS / ERA).
    # One query per slate, keyed by player_id.  PlayerStats may be absent for
    # true rookies — popularity falls through to None values cleanly.
    player_ids = [p.id for p in card_player_map.values()]
    stats_lookup: dict[int, PlayerStats] = {}
    if player_ids:
        stats_rows = (
            db.query(PlayerStats)
            .filter(
                PlayerStats.player_id.in_(player_ids),
                PlayerStats.season == _current_season,
            )
            .all()
        )
        stats_lookup = {row.player_id: row for row in stats_rows}

    # Stage 1: synchronous per-card work — score + env.
    pre_candidates: list[dict] = []
    for card in cards:
        player = card_player_map[f"{card.player_name}|{card.team}"]
        is_pitcher = player.position in PITCHER_POSITIONS

        game = None
        if card.game_id is not None:
            game = game_by_id.get(card.game_id)
        if game is None:
            game = team_to_game.get(card.team.upper())
        if game is None:
            raise ValueError(
                f"Player {card.player_name!r} ({card.team}) has no associated game — "
                "pipeline data integrity error"
            )

        is_two_way_pitcher = False
        if not is_pitcher:
            is_two_way_pitcher = _detect_two_way_pitcher(player, card, game)
            if is_two_way_pitcher:
                is_pitcher = True

        score_result = score_player(
            db,
            player,
            is_pitcher=is_pitcher,
            team_framing_runs=team_framing_lookup.get(card.team.upper()) if is_pitcher else None,
        )

        series_team_w: int | None = None
        series_opp_w: int | None = None
        team_l10: int | None = None

        if is_pitcher:
            is_home = game.home_team.upper() == card.team.upper()

            # Only include the confirmed probable starter for this game.
            # Pitchers on rest from the previous day are still on the active
            # roster and score well — they must be excluded here.
            starter_mlb_id = game.home_starter_mlb_id if is_home else game.away_starter_mlb_id
            starter_name = game.home_starter if is_home else game.away_starter
            if starter_mlb_id is not None:
                if player.mlb_id != starter_mlb_id:
                    continue
            elif starter_name is not None:
                # Token-set equality — same matcher as _detect_two_way_pitcher.
                # The previous substring check let "Smith" match "Smith Jr." in
                # either direction, occasionally including a non-starter pitcher.
                if _tokens(card.player_name) != _tokens(starter_name):
                    continue

            env_score, env_factors = compute_pitcher_env_score(
                **build_pitcher_env_kwargs(game, is_home)
            )
            env_unknown_count = 0  # confirmed starters; env data is reliable

            # The post-resolve append needs these for downstream display.
            series_team_w = game.series_home_wins if is_home else game.series_away_wins
            series_opp_w = game.series_away_wins if is_home else game.series_home_wins
            team_l10 = game.home_team_l10_wins if is_home else game.away_team_l10_wins
        else:
            is_home = game.home_team.upper() == card.team.upper()
            env_score, env_factors, env_unknown_count = compute_batter_env_score(
                **build_batter_env_kwargs(
                    game,
                    is_home,
                    platoon_advantage=card.platoon_advantage,
                    batting_order=card.batting_order,
                )
            )

            series_team_w = game.series_home_wins if is_home else game.series_away_wins
            series_opp_w = game.series_away_wins if is_home else game.series_home_wins
            team_l10 = game.home_team_l10_wins if is_home else game.away_team_l10_wins

        game_id = card.game_id or game.game_id

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

    # Stage 2: assemble FilteredCandidates + predict ownership popularity score.
    candidates: list[FilteredCandidate] = []
    score_min: float | None = None
    score_max: float | None = None
    score_sum: float = 0.0
    for pre in pre_candidates:
        card = pre["card"]
        score_result = pre["score_result"]
        player = pre["player"]
        is_pitcher = pre["is_pitcher"]

        stats = stats_lookup.get(player.id)
        # Rookies get a separate popularity function — the traditional path
        # raises on missing OPS/ERA (a non-rookie missing those is a real
        # data-collection bug), but rookies have no traditional stats by
        # definition.  Routing on PlayerStats.is_rookie_track matches the
        # same predicate the trait engine uses (score_player → score_rookie).
        is_rookie = stats is not None and stats.is_rookie_track
        if is_rookie:
            popularity_score = predict_rookie_popularity_score(
                player_name=card.player_name,
                team=card.team,
                is_pitcher=is_pitcher,
                batting_order=card.batting_order,
                as_of=as_of,
            )
        else:
            popularity_score = predict_popularity_score(
                player_name=card.player_name,
                team=card.team,
                is_pitcher=is_pitcher,
                batting_order=card.batting_order,
                season_ops=stats.ops if stats is not None else None,
                season_era=stats.era if stats is not None else None,
                as_of=as_of,
            )
        score_sum += popularity_score
        score_min = popularity_score if score_min is None else min(score_min, popularity_score)
        score_max = popularity_score if score_max is None else max(score_max, popularity_score)

        candidates.append(FilteredCandidate(
            player_name=card.player_name,
            team=card.team,
            position=player.position,
            total_score=score_result.total_score,
            env_score=pre["env_score"],
            env_factors=pre["env_factors"],
            env_unknown_count=pre.get("env_unknown_count", 0),
            game_id=pre["game_id"],
            is_pitcher=is_pitcher,
            is_two_way_pitcher=pre["is_two_way_pitcher"],
            is_rookie_track=score_result.is_rookie_track,
            traits=score_result.traits,
            batting_order=card.batting_order,
            series_team_wins=pre.get("series_team_wins"),
            series_opp_wins=pre.get("series_opp_wins"),
            team_l10_wins=pre.get("team_l10_wins"),
            predicted_ownership_score=popularity_score,
        ))

    score_mean = score_sum / len(candidates) if candidates else 0.0
    logger.info(
        "Candidate pool: %d cards in → %d candidates out (dropped: %d) | "
        "popularity score min/mean/max: %.2f / %.2f / %.2f",
        len(cards), len(candidates), len(cards) - len(candidates),
        score_min or 0.0, score_mean, score_max or 0.0,
    )

    return candidates
