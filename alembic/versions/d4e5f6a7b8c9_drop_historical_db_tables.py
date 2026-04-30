"""Drop weight_history, draft_lineups, draft_slots — historical-only tables
that no live code reads.  All historical reference data lives in
data/historical_*.csv + historical_slate_results.json.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # draft_slots first (FK references draft_lineups), then draft_lineups
    # SQLite + SQLAlchemy: use op.execute("DROP TABLE IF EXISTS ...") to be
    # idempotent against pre-baseline DBs that may have stamped before these
    # tables existed.
    op.execute("DROP TABLE IF EXISTS draft_slots")
    op.execute("DROP TABLE IF EXISTS draft_lineups")
    op.execute("DROP TABLE IF EXISTS weight_history")


def downgrade() -> None:
    # Recreate the tables exactly as defined in the 792e0bd8996d baseline
    # so a downgrade leaves a structurally-valid (if unused) schema.
    op.create_table(
        "weight_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("player_type", sa.String(), nullable=False),
        sa.Column("weights_json", sa.Text(), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "draft_lineups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("expected_total_value", sa.Float(), nullable=True),
        sa.Column("actual_total_value", sa.Float(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "draft_slots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lineup_id", sa.Integer(), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("slot_mult", sa.Float(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("card_boost", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["lineup_id"], ["draft_lineups.id"]),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
