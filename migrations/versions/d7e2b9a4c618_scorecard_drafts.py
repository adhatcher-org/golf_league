"""Add scorecard drafts: uploads awaiting review.

Revision ID: d7e2b9a4c618
Revises: c3f8a1d27b45
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7e2b9a4c618"
down_revision: str | Sequence[str] | None = "c3f8a1d27b45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scorecard_drafts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("week_id", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("image_path", sa.Text(), nullable=True),
        sa.Column("extracted_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["week_id"], ["weeks.id"], name="fk_scorecard_drafts_week", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], name="fk_scorecard_drafts_user"),
        sa.CheckConstraint("status IN ('extracting', 'ready', 'failed')", name="ck_scorecard_drafts_status"),
    )
    with op.batch_alter_table("scorecard_drafts") as batch:
        batch.create_index("ix_scorecard_drafts_week_id", ["week_id"])


def downgrade() -> None:
    op.drop_table("scorecard_drafts")
