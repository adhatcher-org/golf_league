"""baseline

Revision ID: 496c039e7ac0
Revises:
Create Date: 2026-09-13 16:43:40.271296

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '496c039e7ac0'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
