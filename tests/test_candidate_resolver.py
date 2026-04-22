"""Tests for the candidate resolver kwargs preparation.

Regression coverage for the April 22 bug where _prepare_pitcher_env_kwargs
populated only 'opp_team_stats' and omitted 'opp_team', causing every
pitcher's matchup_quality trait to silently fall back to the 10.0/20
"matchup unknown" neutral baseline regardless of populated SlateGame data.
"""

from app.schemas.filter_strategy import FilterCard, GameEnvironment
from app.services.candidate_resolver import (
    _prepare_pitcher_env_kwargs,
)


def _make_game(
    home: str = "SEA",
    away: str = "OAK",
    home_ops: float | None = 0.740,
    away_ops: float | None = 0.690,
    home_k: float | None = 0.22,
    away_k: float | None = 0.25,
) -> GameEnvironment:
    return GameEnvironment(
        game_id=1,
        home_team=home,
        away_team=away,
        home_team_ops=home_ops,
        away_team_ops=away_ops,
        home_team_k_pct=home_k,
        away_team_k_pct=away_k,
    )


def _make_card(team: str, name: str = "Test Pitcher") -> FilterCard:
    return FilterCard(player_name=name, team=team, position="P", game_id=1)


def test_prepare_pitcher_env_kwargs_populates_both_keys():
    """Both 'opp_team' and 'opp_team_stats' must be set. The scoring engine
    requires both: opp_team alone fails, opp_team_stats alone fails."""
    game = _make_game()
    card = _make_card(team="SEA")

    kwargs = _prepare_pitcher_env_kwargs(game, card)

    assert "opp_team" in kwargs
    assert "opp_team_stats" in kwargs
    assert kwargs["opp_team"] == "OAK"


def test_prepare_pitcher_env_kwargs_home_perspective():
    game = _make_game(home="SEA", away="OAK", home_ops=0.740, away_ops=0.690)
    card = _make_card(team="SEA")

    kwargs = _prepare_pitcher_env_kwargs(game, card)

    assert kwargs["opp_team"] == "OAK"
    assert kwargs["opp_team_stats"]["ops"] == 0.690


def test_prepare_pitcher_env_kwargs_away_perspective():
    game = _make_game(home="SEA", away="OAK", home_ops=0.740, away_ops=0.690)
    card = _make_card(team="OAK")

    kwargs = _prepare_pitcher_env_kwargs(game, card)

    assert kwargs["opp_team"] == "SEA"
    assert kwargs["opp_team_stats"]["ops"] == 0.740


def test_prepare_pitcher_env_kwargs_includes_opp_team_even_when_stats_null():
    """If OPS and K% are both NULL on the SlateGame (shouldn't happen given
    the enrichment validator, but is defined behavior), opp_team is still
    set. That keeps the scorer contract intact; the scorer will then pair
    opp_team with opp_stats=None and fall through to the matchup-unknown
    branch, which is the correct outcome for truly missing data."""
    game = _make_game(home_ops=None, away_ops=None, home_k=None, away_k=None)
    card = _make_card(team="SEA")

    kwargs = _prepare_pitcher_env_kwargs(game, card)

    assert kwargs["opp_team"] == "OAK"
    assert "opp_team_stats" not in kwargs
