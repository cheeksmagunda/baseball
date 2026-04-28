"""add batting_order_source to slate_players

Tracks which source provided the batting order for a SlatePlayer.  Drives
DNP-adjustment confidence and post-slate calibration of which source maps
best to outcomes.  Values: "official", "rotowire_confirmed",
"rotowire_expected", or NULL.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("slate_players", schema=None) as batch_op:
        batch_op.add_column(sa.Column("batting_order_source", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("slate_players", schema=None) as batch_op:
        batch_op.drop_column("batting_order_source")
