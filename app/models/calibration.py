from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CalibrationResult(Base):
    __tablename__ = "calibration_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slate_id: Mapped[int] = mapped_column(Integer, ForeignKey("slates.id"), nullable=False)
    mean_absolute_error: Mapped[float] = mapped_column(Float, nullable=False)
    correlation: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_quintile_hit_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WeightHistory(Base):
    __tablename__ = "weight_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    player_type: Mapped[str] = mapped_column(String, nullable=False)
    weights_json: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
