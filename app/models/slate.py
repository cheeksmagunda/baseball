from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Slate(Base):
    __tablename__ = "slates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    game_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    season_stage: Mapped[str] = mapped_column(String, default="regular")
    status: Mapped[str] = mapped_column(String, default="pending")
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    games: Mapped[list["SlateGame"]] = relationship(back_populates="slate", cascade="all")
    players: Mapped[list["SlatePlayer"]] = relationship(back_populates="slate", cascade="all")


class SlateGame(Base):
    __tablename__ = "slate_games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slate_id: Mapped[int] = mapped_column(Integer, ForeignKey("slates.id"), nullable=False)
    home_team: Mapped[str] = mapped_column(String, nullable=False)
    away_team: Mapped[str] = mapped_column(String, nullable=False)
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    slate: Mapped["Slate"] = relationship(back_populates="games")


class SlatePlayer(Base):
    __tablename__ = "slate_players"
    __table_args__ = (UniqueConstraint("slate_id", "player_id", name="uq_slate_player"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slate_id: Mapped[int] = mapped_column(Integer, ForeignKey("slates.id"), nullable=False)
    player_id: Mapped[int] = mapped_column(Integer, ForeignKey("players.id"), nullable=False)
    card_boost: Mapped[float] = mapped_column(Float, default=0.0)
    drafts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    real_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_highest_value: Mapped[bool] = mapped_column(Boolean, default=False)
    is_most_popular: Mapped[bool] = mapped_column(Boolean, default=False)
    is_most_drafted_3x: Mapped[bool] = mapped_column(Boolean, default=False)

    slate: Mapped["Slate"] = relationship(back_populates="players")
    player: Mapped["Player"] = relationship(foreign_keys=[player_id])
    scores: Mapped[list["PlayerScore"]] = relationship(
        "PlayerScore", back_populates="slate_player", cascade="all"
    )
