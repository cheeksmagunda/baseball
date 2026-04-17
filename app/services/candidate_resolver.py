"""Candidate resolver — converts FilterCards + game environments into
FilteredCandidates ready for the dual-filter optimizer.

Stages:
  1. Stage 0: map each card's (player_name, team) to a Player row in one
     batched SQL query (see find_players_by_name_team_batch).
  2. Stage 1: synchronous work — score traits, compute env score (Filter 2),
     detect two-way pitchers, capture series context.
  3. Stage 2: parallel popularity fetch (Filter 3).
  4. Stage 3: assemble FilteredCandidate instances + emit health-check logs.

Moved out of app/routers/filter_strategy.py so the router stays thin and
this logic is independently testable.
"""

import asyncio
import logging

from sqlalchemy.orm import Session

from app.core.constants import (
    DEFAULT_OPP_K_PCT,
    DEFAULT_OPP_OPS,
    DEFAULT_PITCHER_ERA,
    DEFAULT_PITCHER_WHIP,
    PITCHER_POSITIONS,
    SCORING_K9_CEILING,
    SCORING_K9_FLOOR,
)
from app.core.utils import find_players_by_name_team_batch, get_trait_score
from app.schemas.filter_strategy import FilterCard, GameEnvironment
from app.services.filter_strategy import (
    FilteredCandidate,
    compute_batter_env_score,
    compute_pitcher_env_score,
)
from app.services.popularity import (
    PopularityClass,
    get_popularity_profile,
    reset_url_cache,
)
from app.services.scoring_engine import score_player

logger = logging.getLogger(__name__)


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


def _detect_two_way_pitcher(player, card: FilterCard, game: GameEnvironment | None) -> bool:
    """Check if a non-pitcher (e.g., DH) is the confirmed starter.

    Returns True if detected as a confirmed starter, False otherwise.
    """
    if game is None:
        return False

    is_home = game.home_team.upper() == card.team.upper()
    starter_mlb_id = game.home_starter_mlb_id if is_home else game.away_starter_mlb_id
    starter_name = game.home_starter if is_home else game.away_starter

    if starter_mlb_id is not None and player.mlb_id == starter_mlb_id:
        logger.info(
            "Two-way player detected: %s (%s) is confirmed starter — treating as SP",
            card.player_name, card.team,
        )
        return True

    if starter_name is not None:
        card_name = card.player_name.lower().strip()
        prob_name = starter_name.lower().strip()
        if card_name in prob_name or prob_name in card_name:
            logger.info(
                "Two-way player detected (name match): %s (%s) is confirmed starter — treating as SP",
                card.player_name, card.team,
            )
            return True

    return False


def _prepare_pitcher_env_kwargs(game: GameEnvironment | None, card: FilterCard) -> dict:
    """Extract pitcher environment scoring kwargs from game context."""
    score_kwargs: dict = {}
    if game:
        is_home = game.home_team.upper() == card.team.upper()
        opp_ops = game.away_team_ops if is_home else game.home_team_ops
        opp_k_pct = game.away_team_k_pct if is_home else game.home_team_k_pct
        if opp_ops is not None or opp_k_pct is not None:
            score_kwargs["opp_team_stats"] = {
                "ops": opp_ops if opp_ops is not None else DEFAULT_OPP_OPS,
                "k_pct": opp_k_pct if opp_k_pct is not None else DEFAULT_OPP_K_PCT,
            }
    return score_kwargs


def _prepare_batter_env_kwargs(game: GameEnvironment | None, card: FilterCard) -> dict:
    """Extract batter environment scoring kwargs from game context."""
    score_kwargs: dict = {}
    if game:
        is_home = game.home_team.upper() == card.team.upper()
        opp_era = game.away_starter_era if is_home else game.home_starter_era
        opp_whip = game.away_starter_whip if is_home else game.home_starter_whip
        starter_hand = game.away_starter_hand if is_home else game.home_starter_hand
        if opp_era is not None or opp_whip is not None:
            score_kwargs["opp_pitcher_stats"] = {
                "era": opp_era if opp_era is not None else DEFAULT_PITCHER_ERA,
                "whip": opp_whip if opp_whip is not None else DEFAULT_PITCHER_WHIP,
            }
        score_kwargs["batting_order"] = card.batting_order
        score_kwargs["park_team"] = game.home_team.upper()
        score_kwargs["wind_speed_mph"] = game.wind_speed_mph
        score_kwargs["wind_direction"] = game.wind_direction
        score_kwargs["temperature_f"] = game.temperature_f
        score_kwargs["starter_hand"] = starter_hand
    return score_kwargs


async def resolve_candidates(
    cards: list[FilterCard],
    games: list[GameEnvironment],
    db: Session,
) -> list[FilteredCandidate]:
    """Resolve cards into FilteredCandidates ready for the optimizer.

    See module docstring for the 4-stage flow.
    """
    # Popularity fetchers hit several player-invariant URLs (RSS feeds, daily
    # trends). Reset the slate-wide URL cache so we deduplicate onto a handful
    # of HTTP requests — and avoid serving stale bodies from a previous run.
    reset_url_cache()

    game_by_id, team_to_game = _build_game_lookup(games)

    # Drop cards without a player_name. Draft counts are ingested post-slate,
    # so drafts=None is routine pre-game and should NOT filter the card out.
    cards = [c for c in cards if c.player_name]

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

        series_team_w: int | None = None
        series_opp_w: int | None = None
        team_l10: int | None = None

        if is_pitcher and game:
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
                card_name = card.player_name.lower().strip()
                prob_name = starter_name.lower().strip()
                if card_name not in prob_name and prob_name not in card_name:
                    continue

            opp_ops = game.away_team_ops if is_home else game.home_team_ops
            opp_k_pct = game.away_team_k_pct if is_home else game.home_team_k_pct
            park_team = game.home_team.upper()

            k_rate_score = get_trait_score(score_result.traits, "k_rate")
            k_rate_max = next(
                (t.max_score for t in score_result.traits if t.name == "k_rate"),
                25.0,
            )
            # Reverse the scoring engine's linear K/9 scale (floor → 0 pts, ceiling → max pts).
            k9_range = SCORING_K9_CEILING - SCORING_K9_FLOOR
            pitcher_k9 = (
                SCORING_K9_FLOOR + k_rate_score / k_rate_max * k9_range
                if k_rate_max > 0 else None
            )

            team_ml = game.home_moneyline if is_home else game.away_moneyline

            env_score, env_factors = compute_pitcher_env_score(
                opp_team_ops=opp_ops,
                opp_team_k_pct=opp_k_pct,
                pitcher_k_per_9=pitcher_k9,
                park_team=park_team,
                is_home=is_home,
                team_moneyline=team_ml,
            )
            env_unknown_count = 0  # confirmed starters; env data is reliable
        elif not is_pitcher and game:
            is_home = game.home_team.upper() == card.team.upper()
            opp_era = game.away_starter_era if is_home else game.home_starter_era
            park_team = game.home_team.upper()
            team_ml = game.home_moneyline if is_home else game.away_moneyline
            opp_bp_era = game.away_bullpen_era if is_home else game.home_bullpen_era

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

    # Stage 2: fetch popularity for all players in parallel (Filter 3).
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

    # Stage 3: assemble FilteredCandidates.
    candidates: list[FilteredCandidate] = []
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
            card_boost=card.card_boost,
            total_score=score_result.total_score,
            env_score=pre["env_score"],
            env_factors=pre["env_factors"],
            env_unknown_count=pre.get("env_unknown_count", 0),
            popularity=pop_class,
            game_id=pre["game_id"],
            is_pitcher=pre["is_pitcher"],
            is_two_way_pitcher=pre["is_two_way_pitcher"],
            sharp_score=sharp_score,
            drafts=card.drafts,
            traits=score_result.traits,
            batting_order=card.batting_order,
            series_team_wins=pre.get("series_team_wins"),
            series_opp_wins=pre.get("series_opp_wins"),
            team_l10_wins=pre.get("team_l10_wins"),
        ))

    # Candidate pool health summary.
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
    # trigger at least one scraper. If EVERY player is NEUTRAL, the scrapers
    # are broken and the popularity differential collapses to zero.
    fade_count = sum(1 for c in candidates if c.popularity == PopularityClass.FADE)
    target_count = sum(1 for c in candidates if c.popularity == PopularityClass.TARGET)
    neutral_count = sum(1 for c in candidates if c.popularity == PopularityClass.NEUTRAL)
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
