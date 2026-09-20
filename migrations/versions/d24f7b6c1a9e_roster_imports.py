"""roster imports

Revision ID: d24f7b6c1a9e
Revises: e5a1c3d9f2b7
Create Date: 2026-09-20 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd24f7b6c1a9e'
down_revision: str | Sequence[str] | None = 'e5a1c3d9f2b7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: create `roster_import_batches` and `roster_import_rows`.

    This is the only migration the import tables get in M2 (R-IMPORT-TASKS):
    every column GL-11's and GL-12's review and apply need is created here,
    including `update_opt_in`, both `version` columns and the four result
    counters, even though staging writes nothing but their defaults.
    """
    op.create_table(
        "roster_import_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default="staged"
        ),
        sa.Column("is_initial", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("source_display_name", sa.String(length=255), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_roster_import_batches_created_by_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name="fk_roster_import_batches_course_id_courses",
        ),
    )

    op.create_table(
        "roster_import_rows",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_name", sa.String(length=80), nullable=True),
        sa.Column("last_name", sa.String(length=80), nullable=True),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("tee_label", sa.String(length=30), nullable=True),
        sa.Column("handicap_gold", sa.Integer(), nullable=True),
        sa.Column("handicap_white", sa.Integer(), nullable=True),
        sa.Column("handicap_single", sa.Integer(), nullable=True),
        sa.Column("source_role", sa.String(length=16), nullable=False),
        sa.Column("source_file", sa.String(length=255), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=False),
        sa.Column("raw_line", sa.Text(), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("update_opt_in", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("validation_error", sa.String(length=40), nullable=True),
        sa.Column("warnings", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["roster_import_batches.id"],
            name="fk_roster_import_rows_batch_id_roster_import_batches",
        ),
        sa.UniqueConstraint(
            "batch_id", "position", name="uq_roster_import_rows_batch_position"
        ),
    )

    with op.batch_alter_table("roster_import_rows") as batch_op:
        batch_op.create_index("ix_roster_import_rows_batch_id", ["batch_id"])


def downgrade() -> None:
    """Downgrade schema: drop the two import tables, rows first.

    `roster_import_rows` depends on `roster_import_batches`, so it is
    dropped first.
    """
    with op.batch_alter_table("roster_import_rows") as batch_op:
        batch_op.drop_index("ix_roster_import_rows_batch_id")
    op.drop_table("roster_import_rows")
    op.drop_table("roster_import_batches")
