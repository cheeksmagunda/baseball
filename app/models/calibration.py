from datetime import date

from sqlalchemy import Date, String, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WeightHistory(Base):
    __tablename__ = "weight_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    player_type: Mapped[str] = mapped_column(String, nullable=False)
    weights_json: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
