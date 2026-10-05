"""Add historical weekly handicap snapshots.

Revision ID: 6a2e9d4b7c31
Revises: 9d5b4a7c2e61
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6a2e9d4b7c31"
down_revision: str | Sequence[str] | None = "9d5b4a7c2e61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "week_handicaps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("week_id", sa.Integer(), nullable=False),
        sa.Column("golfer_id", sa.Integer(), nullable=False),
        sa.Column("tee_set_id", sa.Integer(), nullable=False),
        sa.Column("nine", sa.String(5), nullable=False),
        sa.Column("strokes", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(8), nullable=False, server_default="seeded"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["week_id"], ["weeks.id"], name="fk_week_handicaps_week"),
        sa.ForeignKeyConstraint(["golfer_id"], ["golfers.id"], name="fk_week_handicaps_golfer"),
        sa.ForeignKeyConstraint(["tee_set_id"], ["tee_sets.id"], name="fk_week_handicaps_tee"),
        sa.UniqueConstraint("week_id", "golfer_id", name="uq_week_handicaps_week_golfer"),
        sa.CheckConstraint("nine IN ('front','back')", name="ck_week_handicaps_nine"),
        sa.CheckConstraint("source IN ('seeded','carried','computed')", name="ck_week_handicaps_source"),
    )
    with op.batch_alter_table("week_handicaps") as batch:
        for column in ("week_id", "golfer_id", "tee_set_id"):
            batch.create_index(f"ix_week_handicaps_{column}", [column])


def downgrade() -> None:
    op.drop_table("week_handicaps")
