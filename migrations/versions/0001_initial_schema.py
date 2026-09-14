"""Initial PostgreSQL schema.

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


group_status = postgresql.ENUM(
    "active", "paused", "access_lost", "removed", name="group_status", create_type=False
)
scan_job_type = postgresql.ENUM(
    "initial_seven_days", "manual_seven_days", name="scan_job_type", create_type=False
)
work_status = postgresql.ENUM(
    "pending",
    "running",
    "retry",
    "completed",
    "failed",
    "cancelled",
    name="work_status",
    create_type=False,
)
match_source = postgresql.ENUM(
    "live", "history", "recovery", name="match_source", create_type=False
)
notification_status = postgresql.ENUM(
    "pending",
    "running",
    "retry",
    "completed",
    "failed",
    "cancelled",
    name="notification_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    group_status.create(bind, checkfirst=True)
    scan_job_type.create(bind, checkfirst=True)
    work_status.create(bind, checkfirst=True)
    match_source.create(bind, checkfirst=True)
    notification_status.create(bind, checkfirst=True)

    op.create_table(
        "owners",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("notification_chat_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id"),
    )
    op.create_table(
        "tracked_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("status", group_status, server_default="active", nullable=False),
        sa.Column("monitoring_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_contiguous_message_id", sa.BigInteger(), nullable=True),
        sa.Column("configuration_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("access_lost_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["owners.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "telegram_chat_id", name="uq_tracked_groups_owner_chat"),
    )
    op.create_table(
        "keywords",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("normalized_value", sa.String(length=500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["group_id"], ["tracked_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id", "normalized_value", name="uq_keywords_group_normalized_value"
        ),
    )
    op.create_table(
        "conversation_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step", sa.String(length=100), nullable=False),
        sa.Column("draft", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["owners.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id"),
    )
    op.create_table(
        "scan_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", scan_job_type, nullable=False),
        sa.Column("status", work_status, server_default="pending", nullable=False),
        sa.Column("range_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("keyword_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cursor_message_id", sa.BigInteger(), nullable=True),
        sa.Column("messages_checked", sa.Integer(), server_default="0", nullable=False),
        sa.Column("matches_found", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("range_start <= range_end", name="ck_scan_jobs_valid_range"),
        sa.ForeignKeyConstraint(["group_id"], ["tracked_groups.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "message_matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", match_source, nullable=False),
        sa.Column("matched_keywords", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["group_id"], ["tracked_groups.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id", "telegram_message_id", name="uq_message_matches_group_message"
        ),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("match_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", notification_status, server_default="pending", nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_notification_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["match_id"], ["message_matches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id"),
    )

    op.create_index("ix_keywords_group_id", "keywords", ["group_id"])
    op.create_index("ix_conversation_states_expires_at", "conversation_states", ["expires_at"])
    op.create_index(
        "ix_scan_jobs_status_next_attempt_at", "scan_jobs", ["status", "next_attempt_at"]
    )
    op.create_index("ix_message_matches_group_id", "message_matches", ["group_id"])
    op.create_index(
        "ix_notification_outbox_status_next_attempt_at",
        "notification_outbox",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_status_next_attempt_at", table_name="notification_outbox")
    op.drop_index("ix_message_matches_group_id", table_name="message_matches")
    op.drop_index("ix_scan_jobs_status_next_attempt_at", table_name="scan_jobs")
    op.drop_index("ix_conversation_states_expires_at", table_name="conversation_states")
    op.drop_index("ix_keywords_group_id", table_name="keywords")
    op.drop_table("notification_outbox")
    op.drop_table("message_matches")
    op.drop_table("scan_jobs")
    op.drop_table("conversation_states")
    op.drop_table("keywords")
    op.drop_table("tracked_groups")
    op.drop_table("owners")

    bind = op.get_bind()
    notification_status.drop(bind, checkfirst=True)
    match_source.drop(bind, checkfirst=True)
    work_status.drop(bind, checkfirst=True)
    scan_job_type.drop(bind, checkfirst=True)
    group_status.drop(bind, checkfirst=True)
