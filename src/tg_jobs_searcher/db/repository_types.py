"""Shared data transfer types and reporting time windows."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from tg_jobs_searcher.db.models import (
    GroupStatus,
    ScanJobType,
    WorkStatus,
)
from tg_jobs_searcher.services.matching import MatchableKeyword

CONVERSATION_TTL = timedelta(minutes=30)


class ConversationStep(StrEnum):
    AWAITING_GROUP = "awaiting_group"
    AWAITING_KEYWORDS = "awaiting_keywords"
    AWAITING_MODE = "awaiting_mode"
    AWAITING_APPEND_KEYWORDS = "awaiting_append_keywords"
    AWAITING_REPLACE_KEYWORDS = "awaiting_replace_keywords"
    AWAITING_FEEDBACK = "awaiting_feedback"


@dataclass(frozen=True, slots=True)
class GroupSummary:
    id: uuid.UUID
    title: str
    status: GroupStatus


@dataclass(frozen=True, slots=True)
class KeywordSummary:
    id: uuid.UUID
    value: str


@dataclass(frozen=True, slots=True)
class GroupCard:
    id: uuid.UUID
    title: str
    telegram_chat_id: int
    username: str | None
    status: GroupStatus
    keywords: list[KeywordSummary]
    topic_id: int | None = None
    topic_title: str | None = None


@dataclass(frozen=True, slots=True)
class MonitoredGroup:
    id: uuid.UUID
    telegram_chat_id: int
    title: str
    username: str | None
    keywords: list[MatchableKeyword]
    topic_id: int | None = None


@dataclass(frozen=True, slots=True)
class Delivery:
    id: uuid.UUID
    notification_chat_id: int
    payload: str


@dataclass(frozen=True, slots=True)
class ScanJobLease:
    id: uuid.UUID
    group_id: uuid.UUID
    telegram_chat_id: int
    group_title: str
    group_username: str | None
    notification_chat_id: int | None
    job_type: ScanJobType
    range_start: datetime
    range_end: datetime
    keyword_snapshot: list[str | dict[str, Any]]
    cursor_message_id: int | None
    high_watermark_message_id: int | None
    resume_after_message_id: int | None
    topic_id: int | None = None


@dataclass(frozen=True, slots=True)
class ScanMatch:
    telegram_message_id: int
    message_date: datetime
    matched_keywords: list[str]
    payload: str


@dataclass(frozen=True, slots=True)
class ScanProgress:
    continue_scanning: bool
    matches_inserted: int


@dataclass(frozen=True, slots=True)
class ScanCompletion:
    group_title: str
    notification_chat_id: int | None
    job_type: ScanJobType
    messages_checked: int
    matches_found: int


@dataclass(frozen=True, slots=True)
class ScanJobStatus:
    group_title: str
    job_type: ScanJobType
    status: WorkStatus
    messages_checked: int
    matches_found: int
    range_end: datetime
    next_attempt_at: datetime | None


REPORT_TIMEZONE = ZoneInfo("Asia/Tbilisi")
REPORT_HOUR = 9
ON_DEMAND_REPORT_TIMEZONE = timezone(timedelta(hours=3))


def due_report_day(last_reported: date, now: datetime) -> date | None:
    local_now = now.astimezone(REPORT_TIMEZONE)
    latest_due = local_now.date() - timedelta(days=1 if local_now.hour >= REPORT_HOUR else 2)
    next_day = last_reported + timedelta(days=1)
    return next_day if next_day <= latest_due else None


def today_window_utc3(now: datetime) -> tuple[date, datetime, datetime]:
    local_day = now.astimezone(ON_DEMAND_REPORT_TIMEZONE).date()
    start = datetime.combine(local_day, time.min, ON_DEMAND_REPORT_TIMEZONE).astimezone(UTC)
    return local_day, start, now.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DailyAnalytics:
    users_joined: int = 0
    users_blocked: int = 0
    groups_added: int = 0
    groups_removed: int = 0
