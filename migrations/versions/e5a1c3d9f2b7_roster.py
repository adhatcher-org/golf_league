"""roster

Revision ID: e5a1c3d9f2b7
Revises: a3f9c1d2e4b6
Create Date: 2026-09-18 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5a1c3d9f2b7'
down_revision: str | Sequence[str] | None = 'a3f9c1d2e4b6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: create `golfers` and add the FK on `users.golfer_id`.

    SQLite cannot add a constraint to an existing table in place, so the
    `users.golfer_id` foreign key is added with a batch operation. The
    column stays nullable and unique — a user may link to no golfer, and
    no two users may link to the same one.
    """
    op.create_table(
        "golfers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("first_name", sa.String(length=80), nullable=False),
        sa.Column("last_name", sa.String(length=80), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("default_tee_set_id", sa.Integer(), nullable=False),
        sa.Column("handicap_strokes", sa.Integer(), nullable=True),
        sa.Column("handicap_source", sa.String(length=16), nullable=False),
        sa.Column("handicap_status", sa.String(length=16), nullable=False),
        sa.Column("handicap_index", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["default_tee_set_id"],
            ["tee_sets.id"],
            name="fk_golfers_default_tee_set_id_tee_sets",
        ),
        sa.UniqueConstraint("email", name="uq_golfers_email"),
    )

    with op.batch_alter_table("golfers") as batch_op:
        batch_op.create_index("ix_golfers_default_tee_set_id", ["default_tee_set_id"])

    with op.batch_alter_table("users") as batch_op:
        batch_op.create_foreign_key(
            "fk_users_golfer_id_golfers",
            "golfers",
            ["golfer_id"],
            ["id"],
        )


def downgrade() -> None:
    """Downgrade schema: drop the `users.golfer_id` FK, then `golfers`.

    The foreign key on `users.golfer_id` is dropped first so `golfers` has
    no remaining dependent before it is dropped.
    """
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("fk_users_golfer_id_golfers", type_="foreignkey")

    with op.batch_alter_table("golfers") as batch_op:
        batch_op.drop_index("ix_golfers_default_tee_set_id")
    op.drop_table("golfers")
