"""Shared source catalog, personal searches and cross-mode notification identity."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_catalog_search"
down_revision: str | None = "0008_bot_pause"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bot_access_settings", sa.Column("catalog_join_retry_at", sa.DateTime(timezone=True))
    )
    op.create_table(
        "catalog_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("username", sa.String(255)),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("monitoring_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_contiguous_message_id", sa.BigInteger()),
        sa.Column("next_recovery_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(500)),
    )
    op.create_table(
        "catalog_searches",
        sa.Column(
            "owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("owners.id"), primary_key=True
        ),
        sa.Column("keywords", postgresql.JSONB(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("monitoring_started_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "catalog_imports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reference", sa.String(500), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(500)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column(
        "message_matches",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("owners.id")),
    )
    op.add_column("message_matches", sa.Column("telegram_chat_id", sa.BigInteger()))
    op.add_column(
        "message_matches",
        sa.Column(
            "catalog_source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("catalog_sources.id")
        ),
    )
    op.execute("""UPDATE message_matches AS m SET owner_id = g.owner_id,
                  telegram_chat_id = g.telegram_chat_id FROM tracked_groups AS g WHERE m.group_id = g.id""")
    op.alter_column("message_matches", "owner_id", nullable=False)
    op.alter_column("message_matches", "telegram_chat_id", nullable=False)
    op.alter_column("message_matches", "group_id", nullable=True)
    op.create_unique_constraint(
        "uq_message_matches_owner_chat_message",
        "message_matches",
        ["owner_id", "telegram_chat_id", "telegram_message_id"],
    )
    op.create_index("ix_message_matches_catalog_source", "message_matches", ["catalog_source_id"])


def downgrade() -> None:
    op.execute(
        "DELETE FROM notification_outbox WHERE match_id IN (SELECT id FROM message_matches WHERE group_id IS NULL)"
    )
    op.execute("DELETE FROM message_matches WHERE group_id IS NULL")
    op.drop_index("ix_message_matches_catalog_source", "message_matches")
    op.drop_constraint("uq_message_matches_owner_chat_message", "message_matches")
    op.drop_column("message_matches", "catalog_source_id")
    op.drop_column("message_matches", "telegram_chat_id")
    op.drop_column("message_matches", "owner_id")
    op.alter_column("message_matches", "group_id", nullable=False)
    op.drop_table("catalog_imports")
    op.drop_table("catalog_searches")
    op.drop_table("catalog_sources")
    op.drop_column("bot_access_settings", "catalog_join_retry_at")
