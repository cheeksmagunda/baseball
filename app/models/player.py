import unicodedata
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def normalize_name(name: str) -> str:
    """Normalize player name for matching: lowercase, strip accents, collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_name.lower().split())


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (UniqueConstraint("name_normalized", "team", name="uq_player_team"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    name_normalized: Mapped[str] = mapped_column(String, nullable=False, index=True)
    team: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[str] = mapped_column(String, nullable=False)
    mlb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    stats: Mapped[list["PlayerStats"]] = relationship(back_populates="player", cascade="all")
    game_logs: Mapped[list["PlayerGameLog"]] = relationship(
        back_populates="player", cascade="all"
    )


class PlayerStats(Base):
    __tablename__ = "player_stats"
    __table_args__ = (UniqueConstraint("player_id", "season", name="uq_player_season"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    games: Mapped[int] = mapped_column(Integer, default=0)

    # Batter
    pa: Mapped[int] = mapped_column(Integer, default=0)
    ab: Mapped[int] = mapped_column(Integer, default=0)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    hr: Mapped[int] = mapped_column(Integer, default=0)
    rbi: Mapped[int] = mapped_column(Integer, default=0)
    sb: Mapped[int] = mapped_column(Integer, default=0)
    bb: Mapped[int] = mapped_column(Integer, default=0)
    so: Mapped[int] = mapped_column(Integer, default=0)
    avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    ops: Mapped[float | None] = mapped_column(Float, nullable=True)
    iso: Mapped[float | None] = mapped_column(Float, nullable=True)
    barrel_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Pitcher
    ip: Mapped[float] = mapped_column(Float, default=0.0)
    era: Mapped[float | None] = mapped_column(Float, nullable=True)
    whip: Mapped[float | None] = mapped_column(Float, nullable=True)
    k_per_9: Mapped[float | None] = mapped_column(Float, nullable=True)
    bb_per_9: Mapped[float | None] = mapped_column(Float, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    player: Mapped["Player"] = relationship(back_populates="stats")


class PlayerGameLog(Base):
    __tablename__ = "player_game_log"
    __table_args__ = (UniqueConstraint("player_id", "game_date", name="uq_player_game"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    game_date: Mapped[date] = mapped_column(Date, nullable=False)
    opponent: Mapped[str | None] = mapped_column(String, nullable=True)

    # Batter
    ab: Mapped[int] = mapped_column(Integer, default=0)
    runs: Mapped[int] = mapped_column(Integer, default=0)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    hr: Mapped[int] = mapped_column(Integer, default=0)
    rbi: Mapped[int] = mapped_column(Integer, default=0)
    bb: Mapped[int] = mapped_column(Integer, default=0)
    so: Mapped[int] = mapped_column(Integer, default=0)
    sb: Mapped[int] = mapped_column(Integer, default=0)

    # Pitcher
    ip: Mapped[float] = mapped_column(Float, default=0.0)
    er: Mapped[int] = mapped_column(Integer, default=0)
    k_pitching: Mapped[int] = mapped_column(Integer, default=0)
    decision: Mapped[str | None] = mapped_column(String, nullable=True)

    player: Mapped["Player"] = relationship(back_populates="game_logs")
