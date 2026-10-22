"""Add weekly attendance responses.

Revision ID: b4e73c9a1062
Revises: ab74c5d6e105
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4e73c9a1062"
down_revision: str | Sequence[str] | None = "ab74c5d6e105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "week_rsvps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("week_id", sa.Integer(), nullable=False),
        sa.Column("golfer_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["week_id"], ["weeks.id"], name="fk_week_rsvps_week"),
        sa.ForeignKeyConstraint(["golfer_id"], ["golfers.id"], name="fk_week_rsvps_golfer"),
        sa.UniqueConstraint("week_id", "golfer_id", name="uq_week_rsvps_week_golfer"),
        sa.CheckConstraint("status IN ('playing', 'not_playing', 'maybe')", name="ck_week_rsvps_status"),
    )


def downgrade() -> None:
    op.drop_table("week_rsvps")
