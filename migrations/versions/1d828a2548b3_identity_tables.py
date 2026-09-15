"""identity tables

Revision ID: 1d828a2548b3
Revises: 496c039e7ac0
Create Date: 2026-09-15 18:13:57.184475

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '1d828a2548b3'
down_revision: str | Sequence[str] | None = '496c039e7ac0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: create `users` and `user_tokens`.

    `users.golfer_id` is a nullable, unique column only. Its foreign key to
    `golfers.id` is added by GL-10's migration once that table exists; this
    migration must not create a `golfers` table.
    """
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(length=254), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("display_name", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column(
            "session_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("golfer_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("golfer_id", name="uq_users_golfer_id"),
    )

    op.create_table(
        "user_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_tokens_user_id_users"
        ),
        sa.UniqueConstraint("token_digest", name="uq_user_tokens_token_digest"),
    )

    with op.batch_alter_table("users") as batch_op:
        batch_op.create_index("ix_users_username", ["username"])

    with op.batch_alter_table("user_tokens") as batch_op:
        batch_op.create_index("ix_user_tokens_user_id", ["user_id"])
        batch_op.create_index("ix_user_tokens_purpose", ["purpose"])


def downgrade() -> None:
    """Downgrade schema: drop `user_tokens` and `users`."""
    with op.batch_alter_table("user_tokens") as batch_op:
        batch_op.drop_index("ix_user_tokens_purpose")
        batch_op.drop_index("ix_user_tokens_user_id")
    op.drop_table("user_tokens")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index("ix_users_username")
    op.drop_table("users")
