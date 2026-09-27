"""Allow the administrator to grant and revoke bot access.

Revision ID: 0004_multi_user_access
Revises: 0003_group_topics
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_multi_user_access"
down_revision: str | None = "0003_group_topics"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Existing owner rows keep access when upgrading a single-user installation.
    op.add_column(
        "owners",
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("owners", "is_enabled")
