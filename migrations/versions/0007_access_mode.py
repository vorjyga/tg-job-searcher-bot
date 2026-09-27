"""Persist whether new users join freely or by invitation.

Revision ID: 0007_access_mode
Revises: 0006_open_access_analytics
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_access_mode"
down_revision: str | None = "0006_open_access_analytics"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_access_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("access_mode", sa.String(length=16), server_default="open", nullable=False),
        sa.CheckConstraint("id = 1", name="ck_bot_access_settings_singleton"),
        sa.CheckConstraint("access_mode IN ('open', 'invite')", name="ck_bot_access_settings_mode"),
    )
    op.execute("INSERT INTO bot_access_settings (id, access_mode) VALUES (1, 'open')")


def downgrade() -> None:
    op.drop_table("bot_access_settings")
