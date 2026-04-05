from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DraftLineup(Base):
    __tablename__ = "draft_lineups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slate_id: Mapped[int] = mapped_column(Integer, ForeignKey("slates.id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    expected_total_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_total_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    slate: Mapped["Slate"] = relationship(foreign_keys=[slate_id])
    slots: Mapped[list["DraftSlot"]] = relationship(back_populates="lineup", cascade="all")


class DraftSlot(Base):
    __tablename__ = "draft_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lineup_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("draft_lineups.id"), nullable=False, index=True
    )
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    slate_player_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("slate_players.id"), nullable=True, index=True
    )
    player_name: Mapped[str] = mapped_column(String, nullable=False)
    team: Mapped[str | None] = mapped_column(String, nullable=True)
    position: Mapped[str | None] = mapped_column(String, nullable=True)
    real_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    slot_mult: Mapped[float] = mapped_column(Float, nullable=False)
    card_boost: Mapped[float] = mapped_column(Float, default=0.0)

    lineup: Mapped["DraftLineup"] = relationship(back_populates="slots")
