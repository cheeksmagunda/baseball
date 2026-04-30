"""Drop the 7 outcome / display-only columns from slate_players.

The live pipeline never reads these — they were write-only or pure
orphans (no endpoint wrote them).  Removed for "DB stores ONLY current-
cycle live state" — pre-game predictions and during-game env scores
only.  Outcome data lives exclusively in /data/historical_*.csv.

Columns dropped:
    card_boost          — never written by any endpoint (orphan)
    drafts              — never written by any endpoint (orphan)
    real_score          — was written by PUT /api/slates/{date}/results;
                          endpoint deleted in this commit
    total_value         — never written by any endpoint (orphan)
    is_highest_value    — never written by any endpoint (orphan)
    is_most_popular     — never written by any endpoint (orphan)
    is_most_drafted_3x  — never written by any endpoint (orphan)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COLUMNS = [
    "card_boost",
    "drafts",
    "real_score",
    "total_value",
    "is_highest_value",
    "is_most_popular",
    "is_most_drafted_3x",
]


def upgrade() -> None:
    with op.batch_alter_table("slate_players") as batch_op:
        for col in COLUMNS:
            batch_op.drop_column(col)


def downgrade() -> None:
    with op.batch_alter_table("slate_players") as batch_op:
        batch_op.add_column(sa.Column("card_boost", sa.Float(), server_default="0.0", nullable=False))
        batch_op.add_column(sa.Column("drafts", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("real_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("total_value", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("is_highest_value", sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column("is_most_popular", sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column("is_most_drafted_3x", sa.Boolean(), server_default=sa.false(), nullable=False))
