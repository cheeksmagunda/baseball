from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PlayerScore(Base):
    __tablename__ = "player_scores"
    __table_args__ = (UniqueConstraint("slate_player_id", name="uq_player_score_slate_player"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slate_player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("slate_players.id"), nullable=False, index=True
    )
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    slate_player: Mapped["SlatePlayer"] = relationship(back_populates="scores")
    breakdowns: Mapped[list["ScoreBreakdown"]] = relationship(
        back_populates="player_score", cascade="all"
    )


class ScoreBreakdown(Base):
    __tablename__ = "score_breakdowns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_score_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("player_scores.id"), nullable=False, index=True
    )
    trait_name: Mapped[str] = mapped_column(String, nullable=False)
    trait_score: Mapped[float] = mapped_column(Float, nullable=False)
    trait_max: Mapped[float] = mapped_column(Float, nullable=False)
    raw_value: Mapped[str | None] = mapped_column(String, nullable=True)

    player_score: Mapped["PlayerScore"] = relationship(back_populates="breakdowns")
