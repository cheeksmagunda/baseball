from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CachedLineup(Base):
    """Persisted lineup cache so picks survive app restarts."""
    __tablename__ = "cached_lineups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    slate_id: Mapped[int] = mapped_column(Integer, ForeignKey("slates.id"), nullable=False, index=True)
    home_team: Mapped[str] = mapped_column(String, nullable=False)
    away_team: Mapped[str] = mapped_column(String, nullable=False)
    mlb_game_pk: Mapped[int | None] = mapped_column(Integer, nullable=True)  # MLB Stats API game PK
    game_status: Mapped[str | None] = mapped_column(String, nullable=True)  # MLB abstractGameState: Preview, Live, Final
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Pre-game environmental data (Filter 2 — §4.2)
    # Populated before draft time from external sources (Vegas, weather APIs)
    vegas_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_moneyline: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_moneyline: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_starter: Mapped[str | None] = mapped_column(String, nullable=True)
    home_starter_mlb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_starter: Mapped[str | None] = mapped_column(String, nullable=True)
    away_starter_mlb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_starter_era: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_starter_era: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_starter_whip: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_starter_whip: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_starter_k_per_9: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_starter_k_per_9: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_speed_mph: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_direction: Mapped[str | None] = mapped_column(String, nullable=True)
    temperature_f: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_game_time: Mapped[str | None] = mapped_column(String, nullable=True)  # e.g. "7:05 PM ET"
    home_team_ops: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_team_ops: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_team_k_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_team_k_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Bullpen vulnerability data (enriches batter env scoring beyond just the starter)
    home_bullpen_era: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_bullpen_era: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Starter handedness — for conditional matchup scoring (platoon advantage/disadvantage)
    home_starter_hand: Mapped[str | None] = mapped_column(String(1), nullable=True)  # 'L' or 'R'
    away_starter_hand: Mapped[str | None] = mapped_column(String(1), nullable=True)  # 'L' or 'R'

    # Series context — games won by each team in the current series BEFORE today.
    # Populated by enrich_slate_game_series_context() from the MLB schedule API.
    # Used by batter env Group D (momentum) and the momentum gate in _compute_base_ev().
    series_home_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    series_away_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Recent form — wins in last 10 games for each team.
    # Populated alongside series context from the MLB schedule API.
    home_team_l10_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_team_l10_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)

    slate: Mapped["Slate"] = relationship(back_populates="games")

    def get_context_for_team(self, team: str) -> dict:
        """Return home/away context fields keyed from *team*'s perspective.

        Callers no longer need to branch on home_team/away_team manually —
        use this instead of repeating `game.home_X if is_home else game.away_X`.
        """
        is_home = self.home_team.upper() == team.upper()
        return {
            "is_home": is_home,
            "opp_team": self.away_team if is_home else self.home_team,
            "team_moneyline": self.home_moneyline if is_home else self.away_moneyline,
            "opp_team_ops": self.away_team_ops if is_home else self.home_team_ops,
            "opp_team_k_pct": self.away_team_k_pct if is_home else self.home_team_k_pct,
            "opp_starter": self.away_starter if is_home else self.home_starter,
            "opp_starter_mlb_id": self.away_starter_mlb_id if is_home else self.home_starter_mlb_id,
            "opp_starter_era": self.away_starter_era if is_home else self.home_starter_era,
            "opp_bullpen_era": self.away_bullpen_era if is_home else self.home_bullpen_era,
            "series_team_wins": self.series_home_wins if is_home else self.series_away_wins,
            "series_opp_wins": self.series_away_wins if is_home else self.series_home_wins,
            "team_l10_wins": self.home_team_l10_wins if is_home else self.away_team_l10_wins,
        }


class SlatePlayer(Base):
    __tablename__ = "slate_players"
    __table_args__ = (UniqueConstraint("slate_id", "player_id", name="uq_slate_player"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slate_id: Mapped[int] = mapped_column(Integer, ForeignKey("slates.id"), nullable=False, index=True)
    player_id: Mapped[int] = mapped_column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    card_boost: Mapped[float] = mapped_column(Float, default=0.0)
    drafts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    real_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_highest_value: Mapped[bool] = mapped_column(Boolean, default=False)
    is_most_popular: Mapped[bool] = mapped_column(Boolean, default=False)
    is_most_drafted_3x: Mapped[bool] = mapped_column(Boolean, default=False)

    # Pre-game filter fields (§4.2 Filters 2-4, §5.2)
    batting_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Provenance for batting_order — drives DNP-adjustment confidence and
    # post-slate calibration of which source maps best to outcomes.  Values:
    #   "official"            — MLB Stats API boxscore (ground truth)
    #   "rotowire_confirmed"  — RotoWire saw the official card before MLB serialised it
    #   "rotowire_expected"   — RotoWire beat-reporter projection
    #   NULL                  — no source provided a batting order
    batting_order_source: Mapped[str | None] = mapped_column(String, nullable=True)
    platoon_advantage: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    player_status: Mapped[str] = mapped_column(String, default="active")  # active, DNP, scratched, data_missing
    game_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("slate_games.id"), nullable=True, index=True)
    env_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0-1.0, computed by env filter

    slate: Mapped["Slate"] = relationship(back_populates="players")
    player: Mapped["Player"] = relationship(foreign_keys=[player_id])
    scores: Mapped[list["PlayerScore"]] = relationship(
        "PlayerScore", back_populates="slate_player", cascade="all"
    )
