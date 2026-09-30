"""season configuration

Revision ID: c7b4e2d8a91f
Revises: f6a3b7c9d2e1
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7b4e2d8a91f"
down_revision: str | Sequence[str] | None = "f6a3b7c9d2e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "seasons",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("name_override", sa.String(length=120), nullable=True),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("play_weekday", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "status", sa.String(length=8), nullable=False, server_default="draft"
        ),
        sa.Column("first_week_nine", sa.String(length=5), nullable=False),
        sa.CheckConstraint("year >= 1 AND year <= 9999", name="ck_seasons_year_range"),
        sa.CheckConstraint(
            "play_weekday >= 0 AND play_weekday <= 6",
            name="ck_seasons_play_weekday_range",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'complete')", name="ck_seasons_status"
        ),
        sa.CheckConstraint(
            "first_week_nine IN ('front', 'back')",
            name="ck_seasons_first_week_nine",
        ),
        sa.CheckConstraint("start_date <= end_date", name="ck_seasons_dates"),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name="fk_seasons_course_id_courses"
        ),
    )
    with op.batch_alter_table("seasons") as batch_op:
        batch_op.create_index("ix_seasons_course_id", ["course_id"])


def downgrade() -> None:
    with op.batch_alter_table("seasons") as batch_op:
        batch_op.drop_index("ix_seasons_course_id")
    op.drop_table("seasons")
