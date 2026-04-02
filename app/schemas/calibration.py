from pydantic import BaseModel


class WeightsOut(BaseModel):
    pitcher: dict[str, float]
    batter: dict[str, float]


class WeightsIn(BaseModel):
    pitcher: dict[str, float] | None = None
    batter: dict[str, float] | None = None
    notes: str = ""
