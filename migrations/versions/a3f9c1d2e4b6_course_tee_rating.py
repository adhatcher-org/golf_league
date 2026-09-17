"""course tee rating

Revision ID: a3f9c1d2e4b6
Revises: 1d828a2548b3
Create Date: 2026-09-17 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f9c1d2e4b6'
down_revision: str | Sequence[str] | None = '1d828a2548b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: create `courses`, `tee_sets` and `tee_ratings`."""
    op.create_table(
        "courses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("city", sa.String(length=80), nullable=True),
        sa.Column("state", sa.String(length=2), nullable=True),
        sa.Column("website", sa.String(length=255), nullable=True),
        sa.Column(
            "total_holes",
            sa.Integer(),
            nullable=False,
            server_default="18",
        ),
        sa.UniqueConstraint("name", name="uq_courses_name"),
    )

    op.create_table(
        "tee_sets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("color_label", sa.String(length=30), nullable=False),
        sa.Column("gender", sa.String(length=8), nullable=False),
        sa.Column("total_yards", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name="fk_tee_sets_course_id_courses"
        ),
        sa.UniqueConstraint(
            "course_id", "name", "gender", name="uq_tee_sets_course_name_gender"
        ),
    )

    op.create_table(
        "tee_ratings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tee_set_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=5), nullable=False),
        sa.Column("rating", sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column("slope", sa.Integer(), nullable=False),
        sa.Column("par", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tee_set_id"], ["tee_sets.id"], name="fk_tee_ratings_tee_set_id_tee_sets"
        ),
        sa.UniqueConstraint(
            "tee_set_id", "scope", name="uq_tee_ratings_tee_set_scope"
        ),
    )

    with op.batch_alter_table("tee_sets") as batch_op:
        batch_op.create_index("ix_tee_sets_course_id", ["course_id"])

    with op.batch_alter_table("tee_ratings") as batch_op:
        batch_op.create_index("ix_tee_ratings_tee_set_id", ["tee_set_id"])


def downgrade() -> None:
    """Downgrade schema: drop `tee_ratings`, `tee_sets` and `courses`."""
    with op.batch_alter_table("tee_ratings") as batch_op:
        batch_op.drop_index("ix_tee_ratings_tee_set_id")
    op.drop_table("tee_ratings")

    with op.batch_alter_table("tee_sets") as batch_op:
        batch_op.drop_index("ix_tee_sets_course_id")
    op.drop_table("tee_sets")

    op.drop_table("courses")
