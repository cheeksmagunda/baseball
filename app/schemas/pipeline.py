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
