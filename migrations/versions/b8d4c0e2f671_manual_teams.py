"""manual season teams and positions

Revision ID: b8d4c0e2f671
Revises: 7a31b6e9c204
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4c0e2f671"
down_revision: str | Sequence[str] | None = "7a31b6e9c204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], name="fk_teams_season"),
        sa.UniqueConstraint("season_id", "number", name="uq_teams_season_number"),
        sa.UniqueConstraint("id", "season_id", name="uq_teams_id_season"),
    )
    with op.batch_alter_table("teams") as batch_op:
        batch_op.create_index("ix_teams_season_id", ["season_id"])

    op.create_table(
        "team_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("golfer_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("position >= 1 AND position <= 4", name="ck_team_members_position"),
        sa.ForeignKeyConstraint(
            ["team_id", "season_id"], ["teams.id", "teams.season_id"],
            name="fk_team_members_team_season",
        ),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], name="fk_team_members_season"),
        sa.ForeignKeyConstraint(["golfer_id"], ["golfers.id"], name="fk_team_members_golfer"),
        sa.UniqueConstraint("team_id", "position", name="uq_team_members_team_position"),
        sa.UniqueConstraint("team_id", "golfer_id", name="uq_team_members_team_golfer"),
        sa.UniqueConstraint("season_id", "golfer_id", name="uq_team_members_season_golfer"),
    )
    with op.batch_alter_table("team_members") as batch_op:
        batch_op.create_index("ix_team_members_team_id", ["team_id"])
        batch_op.create_index("ix_team_members_season_id", ["season_id"])
        batch_op.create_index("ix_team_members_golfer_id", ["golfer_id"])


def downgrade() -> None:
    with op.batch_alter_table("team_members") as batch_op:
        batch_op.drop_index("ix_team_members_golfer_id")
        batch_op.drop_index("ix_team_members_season_id")
        batch_op.drop_index("ix_team_members_team_id")
    op.drop_table("team_members")
    with op.batch_alter_table("teams") as batch_op:
        batch_op.drop_index("ix_teams_season_id")
    op.drop_table("teams")
