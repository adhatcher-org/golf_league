"""add printed season weeks

Revision ID: ef71a3b9c052
Revises: 3c7d9a1e5b42
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ef71a3b9c052"
down_revision: str | Sequence[str] | None = "3c7d9a1e5b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "weeks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False, quote=True),
        sa.Column("play_date", sa.Date(), nullable=False),
        sa.Column("nine", sa.String(length=5), nullable=True),
        sa.Column("week_type", sa.String(length=16), nullable=False, server_default="match"),
        sa.Column("makeup_for_week_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="scheduled"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint('"index" >= 1', name="ck_weeks_index_positive"),
        sa.CheckConstraint("week_type IN ('match', 'play_with_team', 'rain_date')", name="ck_weeks_type"),
        sa.CheckConstraint("status IN ('scheduled', 'cancelled', 'played')", name="ck_weeks_status"),
        sa.CheckConstraint("nine IS NULL OR nine IN ('front', 'back')", name="ck_weeks_nine"),
        sa.CheckConstraint(
            "(week_type = 'rain_date' AND nine IS NULL) OR "
            "(week_type IN ('match', 'play_with_team') AND nine IS NOT NULL)",
            name="ck_weeks_type_nine",
        ),
        sa.CheckConstraint("makeup_for_week_id IS NULL OR makeup_for_week_id != id", name="ck_weeks_makeup_not_self"),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], name="fk_weeks_season"),
        sa.ForeignKeyConstraint(["makeup_for_week_id"], ["weeks.id"], name="fk_weeks_makeup_for"),
        sa.UniqueConstraint("season_id", "index", name="uq_weeks_season_index"),
    )
    with op.batch_alter_table("weeks") as batch_op:
        batch_op.create_index("ix_weeks_season_id", ["season_id"])
        batch_op.create_index("ix_weeks_makeup_for_week_id", ["makeup_for_week_id"])


def downgrade() -> None:
    with op.batch_alter_table("weeks") as batch_op:
        batch_op.drop_index("ix_weeks_makeup_for_week_id")
        batch_op.drop_index("ix_weeks_season_id")
    op.drop_table("weeks")
