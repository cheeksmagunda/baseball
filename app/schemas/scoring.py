from pydantic import BaseModel


class TraitBreakdown(BaseModel):
    trait_name: str
    score: float
    max_score: float
    raw_value: str | None = None


class PlayerScoreOut(BaseModel):
    player_name: str
    team: str
    position: str
    total_score: float
    estimated_rs_low: float
    estimated_rs_high: float
    estimated_rs_mid: float
    card_boost: float = 0.0
    expected_total_value: float | None = None
    breakdowns: list[TraitBreakdown] = []


class SlateRankingsOut(BaseModel):
    date: str
    player_count: int
    rankings: list[PlayerScoreOut]
