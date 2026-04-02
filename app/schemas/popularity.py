from pydantic import BaseModel


class PopularitySignalOut(BaseModel):
    source: str
    score: float
    context: str


class PopularityProfileOut(BaseModel):
    player_name: str
    team: str
    social_score: float
    news_score: float
    dfs_ownership_score: float
    search_score: float
    sharp_score: float = 0.0
    composite_score: float
    classification: str  # FADE, TARGET, NEUTRAL
    reason: str
    signals: list[PopularitySignalOut]


class PopularityPlayerIn(BaseModel):
    player_name: str
    team: str = ""
    player_score: float = 50.0


class SlatePopularityOut(BaseModel):
    date: str
    player_count: int
    fade_count: int
    target_count: int
    profiles: list[PopularityProfileOut]
