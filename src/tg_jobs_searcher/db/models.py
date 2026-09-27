"""PostgreSQL data model for the Telegram jobs searcher."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tg_jobs_searcher.db.base import Base


class GroupStatus(enum.StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ACCESS_LOST = "access_lost"
    REMOVED = "removed"


class ScanJobType(enum.StrEnum):
    INITIAL_SEVEN_DAYS = "initial_seven_days"
    MANUAL_SEVEN_DAYS = "manual_seven_days"
    RECOVERY = "recovery"


class WorkStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MatchSource(enum.StrEnum):
    LIVE = "live"
    HISTORY = "history"
    RECOVERY = "recovery"


def _enum_values(enum_class: type[enum.StrEnum]) -> list[str]:
    """Persist enum values, which match the lowercase PostgreSQL labels."""
    return [member.value for member in enum_class]


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    notification_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tracked_groups: Mapped[list[TrackedGroup]] = relationship(back_populates="owner")
    conversation_state: Mapped[ConversationState | None] = relationship(back_populates="owner")


class TrackedGroup(Base):
    __tablename__ = "tracked_groups"
    __table_args__ = (
        UniqueConstraint("owner_id", "telegram_chat_id", name="uq_tracked_groups_owner_chat"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("owners.id"), nullable=False)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    topic_id: Mapped[int | None] = mapped_column(BigInteger)
    topic_title: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[GroupStatus] = mapped_column(
        Enum(GroupStatus, name="group_status", values_callable=_enum_values),
        server_default=GroupStatus.ACTIVE.value,
        nullable=False,
    )
    monitoring_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_contiguous_message_id: Mapped[int | None] = mapped_column(BigInteger)
    configuration_version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    access_lost_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    owner: Mapped[Owner] = relationship(back_populates="tracked_groups")
    keywords: Mapped[list[Keyword]] = relationship(back_populates="group")
    scan_jobs: Mapped[list[ScanJob]] = relationship(back_populates="group")
    message_matches: Mapped[list[MessageMatch]] = relationship(back_populates="group")


class Keyword(Base):
    __tablename__ = "keywords"
    __table_args__ = (
        UniqueConstraint("group_id", "normalized_value", name="uq_keywords_group_normalized_value"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracked_groups.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(500), nullable=False)
    terms: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    group: Mapped[TrackedGroup] = relationship(back_populates="keywords")


class ConversationState(Base):
    __tablename__ = "conversation_states"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    step: Mapped[str] = mapped_column(String(100), nullable=False)
    draft: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    owner: Mapped[Owner] = relationship(back_populates="conversation_state")


class ScanJob(Base):
    __tablename__ = "scan_jobs"
    __table_args__ = (CheckConstraint("range_start <= range_end", name="ck_scan_jobs_valid_range"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tracked_groups.id"), nullable=False)
    job_type: Mapped[ScanJobType] = mapped_column(
        Enum(ScanJobType, name="scan_job_type", values_callable=_enum_values), nullable=False
    )
    status: Mapped[WorkStatus] = mapped_column(
        Enum(WorkStatus, name="work_status", values_callable=_enum_values),
        server_default=WorkStatus.PENDING.value,
        nullable=False,
    )
    range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    keyword_snapshot: Mapped[list[str | dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    cursor_message_id: Mapped[int | None] = mapped_column(BigInteger)
    high_watermark_message_id: Mapped[int | None] = mapped_column(BigInteger)
    messages_checked: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    matches_found: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    group: Mapped[TrackedGroup] = relationship(back_populates="scan_jobs")


class MessageMatch(Base):
    __tablename__ = "message_matches"
    __table_args__ = (
        UniqueConstraint(
            "group_id", "telegram_message_id", name="uq_message_matches_group_message"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tracked_groups.id"), nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[MatchSource] = mapped_column(
        Enum(MatchSource, name="match_source", values_callable=_enum_values), nullable=False
    )
    matched_keywords: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    group: Mapped[TrackedGroup] = relationship(back_populates="message_matches")
    notification: Mapped[NotificationOutbox | None] = relationship(back_populates="match")


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("message_matches.id"), unique=True, nullable=False
    )
    status: Mapped[WorkStatus] = mapped_column(
        Enum(WorkStatus, name="notification_status", values_callable=_enum_values),
        server_default=WorkStatus.PENDING.value,
        nullable=False,
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_notification_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    match: Mapped[MessageMatch] = relationship(back_populates="notification")
