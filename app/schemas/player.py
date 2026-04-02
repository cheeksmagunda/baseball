from datetime import date
from pydantic import BaseModel


class PlayerOut(BaseModel):
    id: int
    name: str
    team: str
    position: str
    mlb_id: int | None = None

    model_config = {"from_attributes": True}


class PlayerStatsOut(BaseModel):
    season: int
    games: int = 0
    pa: int = 0
    ab: int = 0
    hits: int = 0
    hr: int = 0
    rbi: int = 0
    sb: int = 0
    avg: float | None = None
    ops: float | None = None
    iso: float | None = None
    barrel_pct: float | None = None
    ip: float = 0.0
    era: float | None = None
    whip: float | None = None
    k_per_9: float | None = None

    model_config = {"from_attributes": True}


class PlayerGameLogOut(BaseModel):
    game_date: date
    opponent: str | None = None
    ab: int = 0
    runs: int = 0
    hits: int = 0
    hr: int = 0
    rbi: int = 0
    bb: int = 0
    so: int = 0
    sb: int = 0
    ip: float = 0.0
    er: int = 0
    k_pitching: int = 0
    decision: str | None = None

    model_config = {"from_attributes": True}


class PlayerDetailOut(PlayerOut):
    stats: list[PlayerStatsOut] = []
    recent_games: list[PlayerGameLogOut] = []
