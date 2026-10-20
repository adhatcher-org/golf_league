"""Add scoring type to seasons.

Revision ID: cfe980076271c31cba6dd6c4
Revises: e41c7f9a2b36
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cfe980076271c31cba6dd6c4"
down_revision: str | Sequence[str] | None = "e41c7f9a2b36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("seasons") as batch_op:
        batch_op.add_column(
            sa.Column(
                "scoring_type",
                sa.String(length=12),
                nullable=False,
                server_default="match_play",
            )
        )
        batch_op.create_check_constraint(
            "ck_seasons_scoring_type",
            "scoring_type IN ('match_play', 'stroke_play')",
        )


def downgrade() -> None:
    with op.batch_alter_table("seasons") as batch_op:
        batch_op.drop_constraint("ck_seasons_scoring_type", type_="check")
        batch_op.drop_column("scoring_type")
