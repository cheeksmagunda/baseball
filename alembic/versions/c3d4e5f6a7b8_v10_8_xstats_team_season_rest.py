"""V10.8 — xStats columns + TeamSeasonStats + opp rest days

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-29 00:00:00.000000

V10.8 sustainable signal additions:
  - PlayerStats: x_woba, x_ba, x_slg (batter), x_era, x_woba_against (pitcher).
    These are Statcast expected-stats (xStats) — industry-standard predictive
    metrics for batter power/contact and pitcher arsenal effectiveness.
  - team_season_stats: NEW table for per-team season aggregates that aren't
    player-keyed.  V10.8 stores catcher framing aggregate (framing_runs,
    framing_strike_pct, framing_pitches); future signals (park-specific
    splits, team-level pitch mix, bullpen xFIP) extend this same table.
  - SlateGame: home_team_rest_days / away_team_rest_days — derived from the
    existing schedule lookback in enrich_slate_game_series_context, so zero
    new MLB API calls.  Per FantasyLabs research, opponent rest days are a
    real DFS edge (back-to-back facing depletes the opposing bullpen).

All new columns nullable so the migration is non-breaking on existing data;
the refresh script + enrichment populate them on next slate cycle.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PLAYER_STATS_NEW_COLUMNS = [
    "x_woba",         # batter expected wOBA
    "x_ba",           # batter expected BA
    "x_slg",          # batter expected SLG
    "x_era",          # pitcher expected ERA
    "x_woba_against", # pitcher expected wOBA against
]

SLATE_GAME_NEW_COLUMNS = [
    "home_team_rest_days",
    "away_team_rest_days",
]


def upgrade() -> None:
    # PlayerStats: xStats columns (Float, nullable)
    with op.batch_alter_table("player_stats", schema=None) as batch_op:
        for col in PLAYER_STATS_NEW_COLUMNS:
            batch_op.add_column(sa.Column(col, sa.Float(), nullable=True))

    # SlateGame: opp rest days (Integer, nullable)
    with op.batch_alter_table("slate_games", schema=None) as batch_op:
        for col in SLATE_GAME_NEW_COLUMNS:
            batch_op.add_column(sa.Column(col, sa.Integer(), nullable=True))

    # team_season_stats: NEW table (catcher framing aggregate is the V10.8 use,
    # but the schema is set up for future per-team signals).
    op.create_table(
        "team_season_stats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team", sa.String(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("framing_runs", sa.Float(), nullable=True),
        sa.Column("framing_strike_pct", sa.Float(), nullable=True),
        sa.Column("framing_pitches", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("team", "season", name="uq_team_season"),
    )
    op.create_index(
        "ix_team_season_stats_team", "team_season_stats", ["team"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_team_season_stats_team", table_name="team_season_stats")
    op.drop_table("team_season_stats")

    with op.batch_alter_table("slate_games", schema=None) as batch_op:
        for col in SLATE_GAME_NEW_COLUMNS:
            batch_op.drop_column(col)

    with op.batch_alter_table("player_stats", schema=None) as batch_op:
        for col in PLAYER_STATS_NEW_COLUMNS:
            batch_op.drop_column(col)
