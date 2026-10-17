"""Add hole scores for submitted scorecards.

Revision ID: c3f8a1d27b45
Revises: ab42c7d9e013
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f8a1d27b45"
down_revision: str | Sequence[str] | None = "ab42c7d9e013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hole_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("player_match_id", sa.Integer(), nullable=False),
        sa.Column("golfer_id", sa.Integer(), nullable=False),
        sa.Column("hole_id", sa.Integer(), nullable=False),
        sa.Column("strokes", sa.Integer(), nullable=False),
        sa.Column("entered_by_user_id", sa.Integer(), nullable=False),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["player_match_id"], ["player_matches.id"], name="fk_hole_scores_player_match"),
        sa.ForeignKeyConstraint(["golfer_id"], ["golfers.id"], name="fk_hole_scores_golfer"),
        sa.ForeignKeyConstraint(["hole_id"], ["holes.id"], name="fk_hole_scores_hole"),
        sa.ForeignKeyConstraint(["entered_by_user_id"], ["users.id"], name="fk_hole_scores_user"),
        sa.UniqueConstraint("player_match_id", "golfer_id", "hole_id", name="uq_hole_scores_match_golfer_hole"),
        sa.CheckConstraint("strokes BETWEEN 1 AND 15", name="ck_hole_scores_strokes"),
    )
    with op.batch_alter_table("hole_scores") as batch:
        for column in ("player_match_id", "golfer_id"):
            batch.create_index(f"ix_hole_scores_{column}", [column])


def downgrade() -> None:
    op.drop_table("hole_scores")
