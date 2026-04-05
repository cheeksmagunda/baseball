from datetime import date
from pydantic import BaseModel


class SlateGameOut(BaseModel):
    home_team: str
    away_team: str
    home_score: int | None = None
    away_score: int | None = None
    vegas_total: float | None = None
    home_moneyline: int | None = None
    away_moneyline: int | None = None
    home_starter: str | None = None
    away_starter: str | None = None
    temperature_f: int | None = None

    model_config = {"from_attributes": True}


class SlatePlayerIn(BaseModel):
    player_name: str
    team: str | None = None
    position: str | None = None
    card_boost: float = 0.0
    batting_order: int | None = None
    platoon_advantage: bool | None = None
    is_debut_or_return: bool = False
    drafts: int | None = None


class SlatePlayerOut(BaseModel):
    id: int
    player_name: str
    team: str
    position: str
    card_boost: float
    real_score: float | None = None
    total_value: float | None = None
    is_highest_value: bool = False
    drafts: int | None = None

    model_config = {"from_attributes": True}


class SlateOut(BaseModel):
    id: int
    date: date
    game_count: int | None = 0
    status: str
    games: list[SlateGameOut] = []
    player_count: int = 0

    model_config = {"from_attributes": True}


class SlateResultsIn(BaseModel):
    """Post-game: upload actual RS values."""
    results: list[dict]  # [{player_name, real_score}]
