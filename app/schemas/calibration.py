from datetime import date
from pydantic import BaseModel


class CalibrationResultOut(BaseModel):
    slate_date: date
    mean_absolute_error: float
    correlation: float | None = None
    top_quintile_hit_rate: float | None = None
    notes: str | None = None

    model_config = {"from_attributes": True}


class WeightsOut(BaseModel):
    pitcher: dict[str, float]
    batter: dict[str, float]


class WeightsIn(BaseModel):
    pitcher: dict[str, float] | None = None
    batter: dict[str, float] | None = None
    notes: str = ""
