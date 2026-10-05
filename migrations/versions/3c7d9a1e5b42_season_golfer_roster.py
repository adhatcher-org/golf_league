"""track golfer inclusion in each season

Revision ID: 3c7d9a1e5b42
Revises: 91c3e7a4b2d6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7d9a1e5b42"
down_revision: str | Sequence[str] | None = "91c3e7a4b2d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "season_golfers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("golfer_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["season_id"], ["seasons.id"], name="fk_season_golfers_season_id_seasons"
        ),
        sa.ForeignKeyConstraint(
            ["golfer_id"], ["golfers.id"], name="fk_season_golfers_golfer_id_golfers"
        ),
        sa.UniqueConstraint(
            "season_id", "golfer_id", name="uq_season_golfers_season_golfer"
        ),
    )
    with op.batch_alter_table("season_golfers") as batch_op:
        batch_op.create_index("ix_season_golfers_season_id", ["season_id"])
        batch_op.create_index("ix_season_golfers_golfer_id", ["golfer_id"])

    # Preserve the existing team/sub candidate pool for seasons created before
    # explicit inclusion existed: active golfers whose default tee fits the
    # season course were previously eligible for assignment.
    op.execute(
        """
        INSERT INTO season_golfers (season_id, golfer_id)
        SELECT DISTINCT seasons.id, golfers.id
        FROM seasons
        JOIN golfers ON golfers.is_active = 1
        WHERE EXISTS (
            SELECT 1 FROM tee_sets
            WHERE tee_sets.course_id = seasons.course_id
              AND (
                tee_sets.id = golfers.default_tee_set_id
                OR tee_sets.color_label = golfers.default_tee_label
              )
        )
        OR EXISTS (
            SELECT 1 FROM team_members
            WHERE team_members.season_id = seasons.id
              AND team_members.golfer_id = golfers.id
        )
        OR EXISTS (
            SELECT 1 FROM season_participants
            WHERE season_participants.season_id = seasons.id
              AND season_participants.golfer_id = golfers.id
        )
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "This migration is forward-only: downgrading would discard season roster selections."
    )
