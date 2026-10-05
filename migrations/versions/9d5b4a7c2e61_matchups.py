"""Add restrictive team pairings and idempotent player slots.

Revision ID: 9d5b4a7c2e61
Revises: ef71a3b9c052
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d5b4a7c2e61"
down_revision: str | Sequence[str] | None = "ef71a3b9c052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "team_matches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("week_id", sa.Integer(), nullable=False),
        sa.Column("home_team_id", sa.Integer(), nullable=False),
        sa.Column("away_team_id", sa.Integer(), nullable=False),
        sa.Column("is_self_match", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["week_id"], ["weeks.id"], name="fk_team_matches_week"),
        sa.ForeignKeyConstraint(["home_team_id"], ["teams.id"], name="fk_team_matches_home"),
        sa.ForeignKeyConstraint(["away_team_id"], ["teams.id"], name="fk_team_matches_away"),
        sa.UniqueConstraint("week_id", "home_team_id", name="uq_team_matches_week_home"),
        sa.UniqueConstraint("week_id", "away_team_id", name="uq_team_matches_week_away"),
        sa.CheckConstraint(
            "(home_team_id = away_team_id AND is_self_match = 1) OR "
            "(home_team_id != away_team_id AND is_self_match = 0)", name="ck_team_matches_self_shape",
        ),
    )
    op.create_table(
        "player_matches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("team_match_id", sa.Integer(), nullable=False),
        sa.Column("position_label", sa.String(5), nullable=False),
        sa.Column("a_golfer_id", sa.Integer(), nullable=False),
        sa.Column("b_golfer_id", sa.Integer(), nullable=False),
        sa.Column("a_is_sub", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("b_is_sub", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("vs_own_handicap", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("manually_adjusted", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["team_match_id"], ["team_matches.id"], name="fk_player_matches_pairing"),
        sa.ForeignKeyConstraint(["a_golfer_id"], ["golfers.id"], name="fk_player_matches_a_golfer"),
        sa.ForeignKeyConstraint(["b_golfer_id"], ["golfers.id"], name="fk_player_matches_b_golfer"),
        sa.UniqueConstraint("team_match_id", "position_label", name="uq_player_matches_pairing_slot"),
        sa.CheckConstraint("position_label IN ('1','2','3','4','P1vP2','P3vP4')", name="ck_player_matches_slot"),
    )
    for table, columns in (("team_matches", ("week_id", "home_team_id", "away_team_id")),
                           ("player_matches", ("team_match_id", "a_golfer_id", "b_golfer_id"))):
        with op.batch_alter_table(table) as batch:
            for column in columns:
                batch.create_index(f"ix_{table}_{column}", [column])


def downgrade() -> None:
    op.drop_table("player_matches")
    op.drop_table("team_matches")
