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
    team: Mapped[str] = mapped_column(String, nullable=False, index=True)
    position: Mapped[str] = mapped_column(String, nullable=False, index=True)
    mlb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    bat_side: Mapped[str | None] = mapped_column(String, nullable=True)   # L, R, or S
    pitch_hand: Mapped[str | None] = mapped_column(String, nullable=True)  # L or R

    stats: Mapped[list["PlayerStats"]] = relationship(back_populates="player", cascade="all")
    game_logs: Mapped[list["PlayerGameLog"]] = relationship(
        back_populates="player", cascade="all"
    )


class PlayerStats(Base):
    __tablename__ = "player_stats"
    __table_args__ = (UniqueConstraint("player_id", "season", name="uq_player_season"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
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
    ops_vs_lhp: Mapped[float | None] = mapped_column(Float, nullable=True)  # OPS vs left-handed pitchers
    ops_vs_rhp: Mapped[float | None] = mapped_column(Float, nullable=True)  # OPS vs right-handed pitchers
    iso: Mapped[float | None] = mapped_column(Float, nullable=True)
    barrel_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Batter Statcast kinematics (from pybaseball → Baseball Savant).
    # Strategy doc §"Offensive Engine": these drive the app's distance multiplier.
    avg_exit_velocity: Mapped[float | None] = mapped_column(Float, nullable=True)   # mph
    max_exit_velocity: Mapped[float | None] = mapped_column(Float, nullable=True)   # mph, single-hit peak
    hard_hit_pct: Mapped[float | None] = mapped_column(Float, nullable=True)        # % of BBE ≥ 95 mph

    # Batter expected stats (V10.8 — Statcast xStats from Baseball Savant).
    # These are the industry-standard predictive metrics: wOBA derived from
    # exit velocity + launch angle (and sprint speed for some batted balls)
    # rather than realised outcomes.  Strategy doc lift: when the live wOBA
    # vs xwOBA gap is wide, xwOBA is the leading indicator.  See
    # https://www.mlb.com/glossary/statcast/expected-woba and
    # https://baseballsavant.mlb.com/leaderboard/expected_statistics.
    x_woba: Mapped[float | None] = mapped_column(Float, nullable=True)              # est_woba (Savant)
    x_ba: Mapped[float | None] = mapped_column(Float, nullable=True)                # est_ba
    x_slg: Mapped[float | None] = mapped_column(Float, nullable=True)               # est_slg

    # Pitcher
    ip: Mapped[float] = mapped_column(Float, default=0.0)
    era: Mapped[float | None] = mapped_column(Float, nullable=True)
    whip: Mapped[float | None] = mapped_column(Float, nullable=True)
    k_per_9: Mapped[float | None] = mapped_column(Float, nullable=True)
    bb_per_9: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Pitcher Statcast kinematics (from pybaseball → Baseball Savant).
    # Strategy doc §"Kinematics of the Pitching Anchor": predict K/9 upside
    # from pitch physics before the live ERA stabilizes (critical for rookies).
    fb_velocity: Mapped[float | None] = mapped_column(Float, nullable=True)         # 4-seam avg, mph
    fb_ivb: Mapped[float | None] = mapped_column(Float, nullable=True)              # induced vertical break, inches
    fb_extension: Mapped[float | None] = mapped_column(Float, nullable=True)        # release extension, feet
    whiff_pct: Mapped[float | None] = mapped_column(Float, nullable=True)           # whiffs / swings
    chase_pct: Mapped[float | None] = mapped_column(Float, nullable=True)           # o-swing%

    # Pitcher expected stats (V10.8 — Statcast xStats from Baseball Savant).
    # xERA is the 1:1 conversion of xwOBA-against onto the ERA scale, and
    # captures arsenal effectiveness independent of BABIP / sequencing luck.
    # Wide ERA-vs-xERA gaps are screaming regression signals (FantasyLabs,
    # PitcherList — see CLAUDE.md V10.8 section for citations).  V10.8 also
    # uses xwOBA-against as the simplified pitch-arsenal-mismatch proxy:
    # the headline number that a pitcher's overall arsenal performs well.
    x_era: Mapped[float | None] = mapped_column(Float, nullable=True)               # ERA-scale xERA
    x_woba_against: Mapped[float | None] = mapped_column(Float, nullable=True)      # est_woba-against

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    player: Mapped["Player"] = relationship(back_populates="stats")


class TeamSeasonStats(Base):
    """Per-team season aggregates that aren't tied to an individual player.

    V10.8 lifecycle: populated by `scripts/refresh_statcast.py` once per
    slate cycle, looked up by env-enrichment in `data_collection.py` and
    by the slate router for display.  All fields are factual season stats
    fetched from public Savant CSVs / pybaseball — they're inputs in the
    same sense as Player.PlayerStats.{era, k_per_9, ...}, NOT slate-day
    outcomes (which would violate the no-historical-bleed rule).

    Currently stores team catcher framing aggregate (V10.8); future
    candidates: park-specific BA/SLG splits, team-level pitch-mix profile,
    bullpen recent xFIP, etc.  Single home for "team-level signal" so the
    schema doesn't sprawl across SlateGame columns.
    """

    __tablename__ = "team_season_stats"
    __table_args__ = (UniqueConstraint("team", "season", name="uq_team_season"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team: Mapped[str] = mapped_column(String, nullable=False, index=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False)

    # Catcher framing (V10.8) — aggregated across all of a team's catchers
    # for the season.  framing_runs = total run value added by framing,
    # framing_strike_pct = % of shadow-zone called pitches converted to
    # strikes.  Reduced impact under 2026 ABS challenge system but still
    # meaningful for the ~98% of pitches that aren't challenged.
    framing_runs: Mapped[float | None] = mapped_column(Float, nullable=True)
    framing_strike_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    framing_pitches: Mapped[int | None] = mapped_column(Integer, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlayerGameLog(Base):
    __tablename__ = "player_game_log"
    __table_args__ = (UniqueConstraint("player_id", "game_date", name="uq_player_game"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    game_date: Mapped[date] = mapped_column(Date, nullable=False)
    opponent: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False, server_default="mlb_api", default="mlb_api")

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
