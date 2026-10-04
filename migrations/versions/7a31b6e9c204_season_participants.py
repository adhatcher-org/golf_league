"""season participant overrides

Revision ID: 7a31b6e9c204
Revises: c7b4e2d8a91f
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a31b6e9c204"
down_revision: str | Sequence[str] | None = "c7b4e2d8a91f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "season_participants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("golfer_id", sa.Integer(), nullable=False),
        sa.Column("tee_set_id", sa.Integer(), nullable=True),
        sa.Column("seed_handicap_strokes", sa.Integer(), nullable=True),
        sa.Column("seed_source", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], name="fk_season_participants_season_id_seasons"),
        sa.ForeignKeyConstraint(["golfer_id"], ["golfers.id"], name="fk_season_participants_golfer_id_golfers"),
        sa.ForeignKeyConstraint(["tee_set_id"], ["tee_sets.id"], name="fk_season_participants_tee_set_id_tee_sets"),
        sa.UniqueConstraint("season_id", "golfer_id", name="uq_season_participants_season_golfer"),
    )
    with op.batch_alter_table("season_participants") as batch_op:
        batch_op.create_index("ix_season_participants_season_id", ["season_id"])
        batch_op.create_index("ix_season_participants_golfer_id", ["golfer_id"])
        batch_op.create_index("ix_season_participants_tee_set_id", ["tee_set_id"])


def downgrade() -> None:
    with op.batch_alter_table("season_participants") as batch_op:
        batch_op.drop_index("ix_season_participants_tee_set_id")
        batch_op.drop_index("ix_season_participants_golfer_id")
        batch_op.drop_index("ix_season_participants_season_id")
    op.drop_table("season_participants")
