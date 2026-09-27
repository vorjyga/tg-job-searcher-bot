"""Open access and durable daily usage events.

Revision ID: 0006_open_access_analytics
Revises: 0005_keyword_rules
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_open_access_analytics"
down_revision: str | None = "0005_keyword_rules"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("owners", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "owners",
        sa.Column("is_bot_blocked", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    # Existing users who have already opened the bot are not counted as new joins.
    op.execute("UPDATE owners SET started_at = created_at WHERE notification_chat_id IS NOT NULL")

    op.create_table(
        "analytics_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_analytics_events_event_type", "analytics_events", ["event_type"])
    op.create_index("ix_analytics_events_occurred_at", "analytics_events", ["occurred_at"])
    op.create_table(
        "analytics_report_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_reported_date", sa.Date(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("analytics_report_state")
    op.drop_index("ix_analytics_events_occurred_at", table_name="analytics_events")
    op.drop_index("ix_analytics_events_event_type", table_name="analytics_events")
    op.drop_table("analytics_events")
    op.drop_column("owners", "is_bot_blocked")
    op.drop_column("owners", "started_at")
