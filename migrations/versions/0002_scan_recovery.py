"""Add recovery scan jobs and their durable high-water mark.

Revision ID: 0002_scan_recovery
Revises: 0001_initial_schema
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_scan_recovery"
down_revision: str | None = "0001_initial_schema"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE scan_job_type ADD VALUE IF NOT EXISTS 'recovery'")
    op.add_column("scan_jobs", sa.Column("high_watermark_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type without rebuilding it.
    op.drop_column("scan_jobs", "high_watermark_message_id")
