"""Schemas for the Filter Strategy API endpoints."""

from pydantic import BaseModel

from app.schemas.scoring import TraitBreakdown


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class GameEnvironment(BaseModel):
    """Pre-game environmental data for a single game."""
    game_id: int | str | None = None
    home_team: str
    away_team: str
    vegas_total: float | None = None
    home_moneyline: int | None = None
    away_moneyline: int | None = None
    home_starter: str | None = None
    home_starter_mlb_id: int | None = None
    away_starter: str | None = None
    away_starter_mlb_id: int | None = None
    home_starter_era: float | None = None
    away_starter_era: float | None = None
    home_starter_whip: float | None = None
    away_starter_whip: float | None = None
    home_starter_k_per_9: float | None = None
    away_starter_k_per_9: float | None = None
    home_team_ops: float | None = None
    away_team_ops: float | None = None
    home_team_k_pct: float | None = None
    away_team_k_pct: float | None = None
    wind_speed_mph: float | None = None
    wind_direction: str | None = None
    temperature_f: int | None = None
    home_bullpen_era: float | None = None
    away_bullpen_era: float | None = None
    series_home_wins: int | None = None
    series_away_wins: int | None = None
    home_team_l10_wins: int | None = None
    away_team_l10_wins: int | None = None


class FilterCard(BaseModel):
    """A player card with all pre-game context for the filter pipeline."""
    player_name: str
    team: str
    position: str
    card_boost: float = 0.0
    game_id: int | str | None = None
    batting_order: int | None = None
    platoon_advantage: bool = False
    drafts: int | None = None  # ownership data


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class StackableGameOut(BaseModel):
    game_id: int | str | None = None
    favored_team: str = ""
    moneyline: int | None = None
    vegas_total: float | None = None
    opp_starter_era: float | None = None


class SlateClassificationOut(BaseModel):
    slate_type: str
    game_count: int
    quality_sp_matchups: int = 0
    high_total_games: int = 0
    blowout_games: int = 0
    stackable_games: list[StackableGameOut] = []
    reason: str = ""


class FilterCandidateOut(BaseModel):
    player_name: str
    team: str
    position: str
    card_boost: float
    total_score: float
    env_score: float
    env_factors: list[str] = []
    popularity: str  # FADE, TARGET, or NEUTRAL (web-scraped)
    is_two_way_pitcher: bool = False  # True if detected as starter despite non-pitcher position (e.g., Ohtani)
    filter_ev: float
    game_id: int | str | None = None
    drafts: int | None = None
    breakdowns: list[TraitBreakdown] = []


class FilterSlotOut(BaseModel):
    slot_index: int
    slot_mult: float
    player_name: str
    team: str
    position: str
    card_boost: float
    total_score: float
    env_score: float
    env_factors: list[str] = []
    popularity: str  # FADE, TARGET, or NEUTRAL (web-scraped)
    is_two_way_pitcher: bool = False  # True if detected as starter despite non-pitcher position (e.g., Ohtani)
    filter_ev: float
    expected_slot_value: float
    game_id: int | str | None = None
    drafts: int | None = None
    breakdowns: list[TraitBreakdown] = []


class FilterLineupOut(BaseModel):
    """A single optimized lineup (Starting 5 or Moonshot)."""
    lineup: list[FilterSlotOut]
    total_expected_value: float
    strategy: str
    composition: dict = {}
    warnings: list[str] = []


class FilterOptimizeResponse(BaseModel):
    slate_classification: SlateClassificationOut
    starting_5: FilterLineupOut
    moonshot: FilterLineupOut
    all_candidates: list[FilterCandidateOut] = []
