"""Add match score states: each match's revision, last change and lock.

Revision ID: e41c7f9a2b36
Revises: d7e2b9a4c618
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e41c7f9a2b36"
down_revision: str | Sequence[str] | None = "d7e2b9a4c618"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "match_score_states",
        sa.Column("player_match_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("scores_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scores_changed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["player_match_id"], ["player_matches.id"],
                                name="fk_match_score_states_player_match"),
        sa.ForeignKeyConstraint(["scores_changed_by_user_id"], ["users.id"],
                                name="fk_match_score_states_changed_by"),
        sa.ForeignKeyConstraint(["locked_by_user_id"], ["users.id"],
                                name="fk_match_score_states_locked_by"),
        sa.CheckConstraint("revision >= 1", name="ck_match_score_states_revision"),
        sa.CheckConstraint("(scores_changed_at IS NULL) = (scores_changed_by_user_id IS NULL)",
                           name="ck_match_score_states_changed_pair"),
        sa.CheckConstraint("(locked_at IS NULL) = (locked_by_user_id IS NULL)",
                           name="ck_match_score_states_locked_pair"),
    )
    # Scores saved before this table existed: each scored match starts at
    # revision 1, last changed when and by whom its latest row was entered
    # (the highest id breaks a tie). No existing row is changed.
    op.execute(
        """
        INSERT INTO match_score_states
            (player_match_id, revision, scores_changed_at, scores_changed_by_user_id)
        SELECT latest.player_match_id, 1, latest.entered_at, latest.entered_by_user_id
        FROM hole_scores AS latest
        WHERE latest.id = (
            SELECT candidate.id FROM hole_scores AS candidate
            WHERE candidate.player_match_id = latest.player_match_id
            ORDER BY candidate.entered_at DESC, candidate.id DESC
            LIMIT 1
        )
        """
    )


def downgrade() -> None:
    op.drop_table("match_score_states")
