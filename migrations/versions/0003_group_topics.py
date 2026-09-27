"""Store an optional Telegram forum topic for each tracked group.

Revision ID: 0003_group_topics
Revises: 0002_scan_recovery
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_group_topics"
down_revision: str | None = "0002_scan_recovery"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tracked_groups", sa.Column("topic_id", sa.BigInteger(), nullable=True))
    op.add_column("tracked_groups", sa.Column("topic_title", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("tracked_groups", "topic_title")
    op.drop_column("tracked_groups", "topic_id")
