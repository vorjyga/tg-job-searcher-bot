"""Persist the administrator-controlled bot pause.

Revision ID: 0008_bot_pause
Revises: 0007_access_mode
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_bot_pause"
down_revision: str | None = "0007_access_mode"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bot_access_settings",
        sa.Column("is_paused", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("bot_access_settings", "is_paused")
