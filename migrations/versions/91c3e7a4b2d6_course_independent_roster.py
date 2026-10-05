"""make golfer tee labels independent of courses

Revision ID: 91c3e7a4b2d6
Revises: b8d4c0e2f671
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "91c3e7a4b2d6"
down_revision: str | Sequence[str] | None = "b8d4c0e2f671"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("golfers") as batch_op:
        batch_op.add_column(
            sa.Column("default_tee_label", sa.String(length=30), nullable=True)
        )
    op.execute(
        "UPDATE golfers SET default_tee_label = "
        "(SELECT color_label FROM tee_sets WHERE tee_sets.id = golfers.default_tee_set_id)"
    )
    op.execute("UPDATE golfers SET default_tee_label = 'White' WHERE default_tee_label IS NULL")
    with op.batch_alter_table("golfers") as batch_op:
        batch_op.alter_column(
            "default_tee_set_id", existing_type=sa.Integer(), nullable=True
        )
        batch_op.alter_column(
            "default_tee_label",
            existing_type=sa.String(length=30),
            nullable=False,
            server_default="White",
        )
    with op.batch_alter_table("roster_import_batches") as batch_op:
        batch_op.alter_column("course_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    raise RuntimeError(
        "This migration is forward-only: a course-independent tee label cannot "
        "be mapped back to one course-specific tee set without user input."
    )
