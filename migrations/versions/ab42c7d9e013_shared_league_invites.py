"""Shared league invite lifecycle and persisted send reservations.

Revision ID: ab42c7d9e013
Revises: 6a2e9d4b7c31
"""

import sqlalchemy as sa
from alembic import op

revision = "ab42c7d9e013"
down_revision = "6a2e9d4b7c31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "league_invite_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("send_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("suppressed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_send_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True)),
        sa.Column("require_phone_last_four", sa.Boolean(), server_default="0", nullable=False),
        *[
            sa.CheckConstraint(f"{name} >= 0", name=f"ck_invite_{name}_nonnegative")
            for name in ("send_count", "suppressed_count", "failed_send_count")
        ],
    )
    op.create_table(
        "golfer_set_password_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("golfer_id", sa.Integer(), sa.ForeignKey("golfers.id"), nullable=False),
        sa.Column("invite_link_id", sa.Integer(), sa.ForeignKey("league_invite_links.id"), nullable=False),
        sa.Column("email_snapshot", sa.String(254), nullable=False),
        sa.Column("expected_user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("token_digest", sa.String(64), unique=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "invite_rate_windows",
        sa.Column("tier", sa.String(16), primary_key=True),
        sa.Column("key_digest", sa.String(64), primary_key=True),
        sa.Column("window_start", sa.Float(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint("count >= 0", name="ck_invite_window_count_nonnegative"),
        sa.CheckConstraint("tier IN ('email', 'ip', 'invite', 'global')", name="ck_invite_window_tier"),
    )


def downgrade() -> None:
    op.drop_table("invite_rate_windows")
    op.drop_table("golfer_set_password_tokens")
    op.drop_table("league_invite_links")
