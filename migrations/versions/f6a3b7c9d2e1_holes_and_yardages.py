"""holes and yardages

Revision ID: f6a3b7c9d2e1
Revises: d24f7b6c1a9e
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a3b7c9d2e1"
down_revision: str | Sequence[str] | None = "d24f7b6c1a9e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "holes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("nine", sa.String(length=5), nullable=False),
        sa.Column("par", sa.Integer(), nullable=False),
        sa.Column("stroke_index_18", sa.Integer(), nullable=False),
        sa.Column("stroke_index_9", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], name="fk_holes_course_id_courses"),
        sa.UniqueConstraint("course_id", "number", name="uq_holes_course_number"),
    )
    op.create_table(
        "hole_yardages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("hole_id", sa.Integer(), nullable=False),
        sa.Column("tee_set_id", sa.Integer(), nullable=False),
        sa.Column("yards", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["hole_id"], ["holes.id"], name="fk_hole_yardages_hole_id_holes"),
        sa.ForeignKeyConstraint(["tee_set_id"], ["tee_sets.id"], name="fk_hole_yardages_tee_set_id_tee_sets"),
        sa.UniqueConstraint("hole_id", "tee_set_id", name="uq_hole_yardages_hole_tee_set"),
    )


def downgrade() -> None:
    op.drop_table("hole_yardages")
    op.drop_table("holes")
