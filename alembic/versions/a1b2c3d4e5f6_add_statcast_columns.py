"""add_statcast_columns

Revision ID: a1b2c3d4e5f6
Revises: 3d6c88294f54
Create Date: 2026-04-21 00:00:00.000000

V10.0 — add Statcast kinematic metrics to PlayerStats.
Batter: avg/max exit velocity, hard-hit %.
Pitcher: fastball velo, IVB, extension, whiff %, chase %.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "3d6c88294f54"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


STATCAST_COLUMNS = [
    "avg_exit_velocity",
    "max_exit_velocity",
    "hard_hit_pct",
    "fb_velocity",
    "fb_ivb",
    "fb_extension",
    "whiff_pct",
    "chase_pct",
]


def upgrade() -> None:
    with op.batch_alter_table("player_stats", schema=None) as batch_op:
        for col in STATCAST_COLUMNS:
            batch_op.add_column(sa.Column(col, sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("player_stats", schema=None) as batch_op:
        for col in STATCAST_COLUMNS:
            batch_op.drop_column(col)
