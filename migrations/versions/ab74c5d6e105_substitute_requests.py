"""Add player-initiated substitute requests.

Revision ID: ab74c5d6e105
Revises: cfe980076271c31cba6dd6c4
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ab74c5d6e105"
down_revision: str | Sequence[str] | None = "cfe980076271c31cba6dd6c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "substitute_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("week_id", sa.Integer(), nullable=False),
        sa.Column("player_match_id", sa.Integer(), nullable=False),
        sa.Column("requesting_golfer_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="pending"),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["week_id"], ["weeks.id"], name="fk_substitute_requests_week"),
        sa.ForeignKeyConstraint(["player_match_id"], ["player_matches.id"], name="fk_substitute_requests_match"),
        sa.ForeignKeyConstraint(["requesting_golfer_id"], ["golfers.id"], name="fk_substitute_requests_golfer"),
        sa.UniqueConstraint("player_match_id", name="uq_substitute_requests_player_match"),
        sa.UniqueConstraint("token_digest", name="uq_substitute_requests_token_digest"),
        sa.CheckConstraint("status IN ('pending', 'fulfilled', 'declined')", name="ck_substitute_requests_status"),
    )
    op.create_index("ix_substitute_requests_week_id", "substitute_requests", ["week_id"])


def downgrade() -> None:
    op.drop_index("ix_substitute_requests_week_id", table_name="substitute_requests")
    op.drop_table("substitute_requests")
