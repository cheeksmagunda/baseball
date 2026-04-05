from pydantic import BaseModel


class PlayerSummary(BaseModel):
    name: str
    score: float


class FetchResult(BaseModel):
    date: str
    game_count: int | None = None
    status: str


class ScoreResult(BaseModel):
    date: str
    scored: int
    top_5: list[PlayerSummary]


class StatsResult(BaseModel):
    fetched: int
    failed: int


class PipelineResult(BaseModel):
    date: str
    schedule: FetchResult
    stats: StatsResult
    scored_players: int
    top_5: list[PlayerSummary]


class FilterStrategySlotOut(BaseModel):
    slot: int
    slot_mult: float
    player: str
    team: str
    position: str
    boost: float
    score: float
    env_score: float
    env_factors: list[str] = []
    ownership: str
    filter_ev: float
    slot_value: float


class FilterStrategyPipelineResult(BaseModel):
    date: str
    slate_type: str
    slate_reason: str
    composition: dict = {}
    total_expected_value: float
    warnings: list[str] = []
    lineup: list[FilterStrategySlotOut]
    candidate_count: int
