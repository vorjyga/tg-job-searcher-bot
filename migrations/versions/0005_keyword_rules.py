"""Store rule terms so old keywords remain literal, including ampersands.

Revision ID: 0005_keyword_rules
Revises: 0004_multi_user_access
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_keyword_rules"
down_revision: str | None = "0004_multi_user_access"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("keywords", sa.Column("terms", postgresql.JSONB(), nullable=True))
    op.execute("UPDATE keywords SET terms = jsonb_build_array(normalized_value)")
    # Show legacy literals using the new syntax without changing what they match.
    op.execute(
        """
        UPDATE keywords
        SET value = '"' || replace(value, '"', '""') || '"'
        WHERE (strpos(value, '&') > 0 OR strpos(value, '"') > 0)
          AND length(value) + 2 + length(value) - length(replace(value, '"', '')) <= 500
        """
    )
    op.alter_column("keywords", "terms", nullable=False)


def downgrade() -> None:
    op.drop_column("keywords", "terms")
