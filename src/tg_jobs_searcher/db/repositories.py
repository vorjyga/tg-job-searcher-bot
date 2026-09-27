"""PostgreSQL repositories for owners, dialogs and tracked-group configuration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.db.models import (
    ConversationState,
    GroupStatus,
    Keyword,
    MatchSource,
    MessageMatch,
    NotificationOutbox,
    Owner,
    ScanJob,
    ScanJobType,
    TrackedGroup,
    WorkStatus,
)
from tg_jobs_searcher.services.keywords import KeywordInput
from tg_jobs_searcher.services.matching import MatchableKeyword
from tg_jobs_searcher.telegram.client import ResolvedGroup

CONVERSATION_TTL = timedelta(minutes=30)


class ConversationStep(StrEnum):
    AWAITING_GROUP = "awaiting_group"
    AWAITING_KEYWORDS = "awaiting_keywords"
    AWAITING_MODE = "awaiting_mode"
    AWAITING_APPEND_KEYWORDS = "awaiting_append_keywords"
    AWAITING_REPLACE_KEYWORDS = "awaiting_replace_keywords"


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


@dataclass(frozen=True, slots=True)
class MonitoredGroup:
    id: uuid.UUID
    telegram_chat_id: int
    title: str
    username: str | None
    keywords: list[MatchableKeyword]


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
    keyword_snapshot: list[str]
    cursor_message_id: int | None
    high_watermark_message_id: int | None
    resume_after_message_id: int | None


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


class OwnerRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ensure_owner(self, telegram_user_id: int, notification_chat_id: int) -> Owner:
        statement = (
            insert(Owner)
            .values(
                telegram_user_id=telegram_user_id,
                notification_chat_id=notification_chat_id,
            )
            .on_conflict_do_update(
                index_elements=[Owner.telegram_user_id],
                set_={
                    "notification_chat_id": notification_chat_id,
                    "updated_at": func.now(),
                },
            )
            .returning(Owner)
        )
        async with self._session_factory() as session:
            owner = (await session.execute(statement)).scalar_one()
            await session.commit()
            return owner


class ConversationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def set(
        self,
        owner_id: uuid.UUID,
        step: ConversationStep,
        draft: dict[str, Any],
    ) -> None:
        expires_at = datetime.now(UTC) + CONVERSATION_TTL
        statement = (
            insert(ConversationState)
            .values(owner_id=owner_id, step=step.value, draft=draft, expires_at=expires_at)
            .on_conflict_do_update(
                index_elements=[ConversationState.owner_id],
                set_={
                    "step": step.value,
                    "draft": draft,
                    "expires_at": expires_at,
                    "updated_at": func.now(),
                },
            )
        )
        async with self._session_factory() as session:
            await session.execute(statement)
            await session.commit()

    async def get(self, owner_id: uuid.UUID) -> ConversationState | None:
        async with self._session_factory() as session:
            state = await session.scalar(
                select(ConversationState).where(ConversationState.owner_id == owner_id)
            )
            if state is None:
                return None
            if state.expires_at > datetime.now(UTC):
                return state
            await session.delete(state)
            await session.commit()
            return None

    async def clear(self, owner_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(ConversationState).where(ConversationState.owner_id == owner_id)
            )
            await session.commit()
            return result.rowcount > 0


class GroupRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_active_by_telegram_id(
        self, owner_id: uuid.UUID, telegram_chat_id: int
    ) -> GroupCard | None:
        async with self._session_factory() as session:
            group = await session.scalar(
                select(TrackedGroup).where(
                    TrackedGroup.owner_id == owner_id,
                    TrackedGroup.telegram_chat_id == telegram_chat_id,
                    TrackedGroup.status != GroupStatus.REMOVED,
                )
            )
            if group is None:
                return None
            return await _load_card(session, group)

    async def list_groups(self, owner_id: uuid.UUID) -> list[GroupSummary]:
        async with self._session_factory() as session:
            groups = await session.scalars(
                select(TrackedGroup)
                .where(
                    TrackedGroup.owner_id == owner_id,
                    TrackedGroup.status != GroupStatus.REMOVED,
                )
                .order_by(TrackedGroup.title, TrackedGroup.id)
            )
            return [
                GroupSummary(id=group.id, title=group.title, status=group.status)
                for group in groups
            ]

    async def get_card(self, owner_id: uuid.UUID, group_id: uuid.UUID) -> GroupCard | None:
        async with self._session_factory() as session:
            group = await session.scalar(
                select(TrackedGroup).where(
                    TrackedGroup.id == group_id,
                    TrackedGroup.owner_id == owner_id,
                    TrackedGroup.status != GroupStatus.REMOVED,
                )
            )
            if group is None:
                return None
            return await _load_card(session, group)

    async def create_or_reactivate(
        self,
        owner_id: uuid.UUID,
        resolved_group: ResolvedGroup,
        keywords: list[KeywordInput],
        *,
        scan_history: bool,
    ) -> GroupCard:
        async with self._session_factory() as session, session.begin():
            group = await session.scalar(
                select(TrackedGroup)
                .where(
                    TrackedGroup.owner_id == owner_id,
                    TrackedGroup.telegram_chat_id == resolved_group.telegram_chat_id,
                )
                .with_for_update()
            )
            if group is None:
                group = TrackedGroup(
                    owner_id=owner_id,
                    telegram_chat_id=resolved_group.telegram_chat_id,
                    title=resolved_group.title,
                    username=resolved_group.username,
                    status=GroupStatus.ACTIVE,
                    monitoring_started_at=datetime.now(UTC),
                )
                session.add(group)
                await session.flush()
            elif group.status == GroupStatus.REMOVED:
                group.title = resolved_group.title
                group.username = resolved_group.username
                group.status = GroupStatus.ACTIVE
                group.deleted_at = None
                group.monitoring_started_at = datetime.now(UTC)
                group.configuration_version += 1
                await session.execute(delete(Keyword).where(Keyword.group_id == group.id))
            else:
                raise ValueError("This group is already tracked")
            session.add_all(_make_keyword_models(group.id, keywords))
            if scan_history:
                range_end = datetime.now(UTC)
                session.add(
                    ScanJob(
                        group_id=group.id,
                        job_type=ScanJobType.INITIAL_SEVEN_DAYS,
                        range_start=range_end - timedelta(days=7),
                        range_end=range_end,
                        keyword_snapshot=[keyword.normalized_value for keyword in keywords],
                    )
                )
            group_id = group.id
        card = await self.get_card(owner_id, group_id)
        assert card is not None
        return card

    async def append_keywords(
        self, owner_id: uuid.UUID, group_id: uuid.UUID, keywords: list[KeywordInput]
    ) -> GroupCard | None:
        async with self._session_factory() as session, session.begin():
            group = await _get_active_group_for_update(session, owner_id, group_id)
            if group is None:
                return None
            statement = insert(Keyword).values(
                [
                    {
                        "group_id": group.id,
                        "value": keyword.value,
                        "normalized_value": keyword.normalized_value,
                    }
                    for keyword in keywords
                ]
            )
            statement = statement.on_conflict_do_nothing(
                constraint="uq_keywords_group_normalized_value"
            )
            await session.execute(statement)
            group.configuration_version += 1
            if group.status == GroupStatus.PAUSED:
                group.status = GroupStatus.ACTIVE
        return await self.get_card(owner_id, group_id)

    async def replace_keywords(
        self, owner_id: uuid.UUID, group_id: uuid.UUID, keywords: list[KeywordInput]
    ) -> GroupCard | None:
        async with self._session_factory() as session, session.begin():
            group = await _get_active_group_for_update(session, owner_id, group_id)
            if group is None:
                return None
            await session.execute(delete(Keyword).where(Keyword.group_id == group.id))
            session.add_all(_make_keyword_models(group.id, keywords))
            group.configuration_version += 1
            group.status = GroupStatus.ACTIVE
        return await self.get_card(owner_id, group_id)

    async def delete_keyword(self, owner_id: uuid.UUID, keyword_id: uuid.UUID) -> GroupCard | None:
        async with self._session_factory() as session, session.begin():
            keyword = await session.scalar(
                select(Keyword)
                .join(TrackedGroup)
                .where(
                    Keyword.id == keyword_id,
                    TrackedGroup.owner_id == owner_id,
                    TrackedGroup.status != GroupStatus.REMOVED,
                )
                .with_for_update()
            )
            if keyword is None:
                return None
            group = await _get_active_group_for_update(session, owner_id, keyword.group_id)
            if group is None:
                return None
            group_id = group.id
            await session.delete(keyword)
            await session.flush()
            remaining = await session.scalar(
                select(func.count()).select_from(Keyword).where(Keyword.group_id == group_id)
            )
            group.configuration_version += 1
            if remaining == 0:
                group.status = GroupStatus.PAUSED
        return await self.get_card(owner_id, group_id)

    async def remove_group(self, owner_id: uuid.UUID, group_id: uuid.UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            group = await _get_active_group_for_update(session, owner_id, group_id)
            if group is None:
                return False
            group.status = GroupStatus.REMOVED
            group.deleted_at = datetime.now(UTC)
            group.configuration_version += 1
            await session.execute(
                update(ScanJob)
                .where(
                    ScanJob.group_id == group_id,
                    ScanJob.status.in_([WorkStatus.PENDING, WorkStatus.RUNNING, WorkStatus.RETRY]),
                )
                .values(status=WorkStatus.CANCELLED, completed_at=datetime.now(UTC))
            )
            await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.match_id.in_(
                        select(MessageMatch.id).where(MessageMatch.group_id == group_id)
                    ),
                    NotificationOutbox.status.in_(
                        [WorkStatus.PENDING, WorkStatus.RUNNING, WorkStatus.RETRY]
                    ),
                )
                .values(status=WorkStatus.CANCELLED)
            )
            return True

    async def restore_access(
        self, owner_id: uuid.UUID, group_id: uuid.UUID, resolved_group: ResolvedGroup
    ) -> GroupCard | None:
        async with self._session_factory() as session, session.begin():
            group = await _get_active_group_for_update(session, owner_id, group_id)
            if group is None or group.telegram_chat_id != resolved_group.telegram_chat_id:
                return None
            group.title = resolved_group.title
            group.username = resolved_group.username
            group.status = GroupStatus.ACTIVE
            group.access_lost_notified_at = None
        return await self.get_card(owner_id, group_id)


class MonitoringRepository:
    """Read current group configuration and persist a live match atomically."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def active_groups_for_chat(
        self, telegram_chat_id: int, message_date: datetime
    ) -> list[MonitoredGroup]:
        async with self._session_factory() as session:
            groups = await session.scalars(
                select(TrackedGroup)
                .where(
                    TrackedGroup.telegram_chat_id == telegram_chat_id,
                    TrackedGroup.status == GroupStatus.ACTIVE,
                    TrackedGroup.monitoring_started_at.is_not(None),
                    TrackedGroup.monitoring_started_at <= message_date,
                )
                .order_by(TrackedGroup.id)
            )
            result: list[MonitoredGroup] = []
            for group in groups:
                keywords = await session.scalars(
                    select(Keyword)
                    .where(Keyword.group_id == group.id)
                    .order_by(Keyword.created_at, Keyword.id)
                )
                result.append(
                    MonitoredGroup(
                        id=group.id,
                        telegram_chat_id=group.telegram_chat_id,
                        title=group.title,
                        username=group.username,
                        keywords=[
                            MatchableKeyword(
                                value=keyword.value,
                                normalized_value=keyword.normalized_value,
                            )
                            for keyword in keywords
                        ],
                    )
                )
            return result

    async def record_live_match(
        self,
        *,
        group_id: uuid.UUID,
        telegram_message_id: int,
        message_date: datetime,
        matched_keywords: list[str],
        payload: str,
    ) -> bool:
        """Write match and outbox row together; duplicate updates become no-ops."""
        async with self._session_factory() as session, session.begin():
            group = await session.scalar(
                select(TrackedGroup)
                .where(
                    TrackedGroup.id == group_id,
                    TrackedGroup.status == GroupStatus.ACTIVE,
                    TrackedGroup.monitoring_started_at.is_not(None),
                    TrackedGroup.monitoring_started_at <= message_date,
                )
                .with_for_update()
            )
            if group is None:
                return False
            statement = (
                insert(MessageMatch)
                .values(
                    group_id=group_id,
                    telegram_message_id=telegram_message_id,
                    message_date=message_date,
                    source=MatchSource.LIVE,
                    matched_keywords=matched_keywords,
                )
                .on_conflict_do_nothing(constraint="uq_message_matches_group_message")
                .returning(MessageMatch.id)
            )
            match_id = (await session.execute(statement)).scalar_one_or_none()
            if match_id is None:
                return False
            session.add(NotificationOutbox(match_id=match_id, payload=payload))
            return True


class ScanRepository:
    """PostgreSQL queue and checkpoint operations for historical scans."""

    _MAX_ATTEMPTS = 5

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def requeue_running(self) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(ScanJob)
                .where(ScanJob.status == WorkStatus.RUNNING)
                .values(status=WorkStatus.RETRY, next_attempt_at=datetime.now(UTC))
            )

    async def create_manual_scan(
        self, owner_id: uuid.UUID, group_id: uuid.UUID
    ) -> tuple[ScanJob | None, bool]:
        """Create one manual seven-day scan, or return the existing active job."""
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            group = await _get_active_group_for_update(session, owner_id, group_id)
            if group is None:
                return None, False
            active_job = await session.scalar(
                select(ScanJob)
                .where(
                    ScanJob.group_id == group.id,
                    ScanJob.status.in_([WorkStatus.PENDING, WorkStatus.RUNNING, WorkStatus.RETRY]),
                )
                .order_by(ScanJob.created_at)
            )
            if active_job is not None:
                return active_job, False
            job = ScanJob(
                group_id=group.id,
                job_type=ScanJobType.MANUAL_SEVEN_DAYS,
                range_start=now - timedelta(days=7),
                range_end=now,
                keyword_snapshot=await _keyword_snapshot(session, group.id),
            )
            session.add(job)
            await session.flush()
            return job, True

    async def schedule_recovery_jobs(self) -> int:
        """Queue a bounded catch-up scan for every active group after process startup."""
        now = datetime.now(UTC)
        created = 0
        async with self._session_factory() as session, session.begin():
            groups = await session.scalars(
                select(TrackedGroup)
                .where(
                    TrackedGroup.status == GroupStatus.ACTIVE,
                    TrackedGroup.monitoring_started_at.is_not(None),
                )
                .with_for_update()
            )
            for group in groups:
                active_job = await session.scalar(
                    select(ScanJob.id).where(
                        ScanJob.group_id == group.id,
                        ScanJob.status.in_([
                            WorkStatus.PENDING,
                            WorkStatus.RUNNING,
                            WorkStatus.RETRY,
                        ]),
                    )
                )
                if active_job is not None:
                    continue
                monitoring_started_at = group.monitoring_started_at
                assert monitoring_started_at is not None
                session.add(
                    ScanJob(
                        group_id=group.id,
                        job_type=ScanJobType.RECOVERY,
                        range_start=max(now - timedelta(days=7), monitoring_started_at),
                        range_end=now,
                        keyword_snapshot=await _keyword_snapshot(session, group.id),
                    )
                )
                created += 1
        return created

    async def claim_next_job(self) -> ScanJobLease | None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            row = await session.execute(
                select(ScanJob, TrackedGroup, Owner.notification_chat_id)
                .join(TrackedGroup, ScanJob.group_id == TrackedGroup.id)
                .join(Owner, TrackedGroup.owner_id == Owner.id)
                .where(
                    ScanJob.status.in_([WorkStatus.PENDING, WorkStatus.RETRY]),
                    or_(ScanJob.next_attempt_at.is_(None), ScanJob.next_attempt_at <= now),
                    TrackedGroup.status == GroupStatus.ACTIVE,
                )
                .order_by(ScanJob.created_at, ScanJob.id)
                .limit(1)
                .with_for_update(skip_locked=True, of=ScanJob)
            )
            claimed = row.first()
            if claimed is None:
                return None
            job, group, notification_chat_id = claimed
            job.status = WorkStatus.RUNNING
            job.attempt_count += 1
            job.next_attempt_at = None
            if job.started_at is None:
                job.started_at = now
            return ScanJobLease(
                id=job.id,
                group_id=group.id,
                telegram_chat_id=group.telegram_chat_id,
                group_title=group.title,
                group_username=group.username,
                notification_chat_id=notification_chat_id,
                job_type=job.job_type,
                range_start=job.range_start,
                range_end=job.range_end,
                keyword_snapshot=[value for value in job.keyword_snapshot if isinstance(value, str)],
                cursor_message_id=job.cursor_message_id,
                high_watermark_message_id=job.high_watermark_message_id,
                resume_after_message_id=(
                    group.last_contiguous_message_id
                    if job.job_type == ScanJobType.RECOVERY and job.cursor_message_id is None
                    else None
                ),
            )

    async def persist_page(
        self,
        *,
        job_id: uuid.UUID,
        cursor_message_id: int,
        high_watermark_message_id: int | None,
        messages_checked: int,
        matches: list[ScanMatch],
    ) -> ScanProgress:
        """Persist an entire history page before advancing its resume cursor."""
        async with self._session_factory() as session, session.begin():
            row = await session.execute(
                select(ScanJob, TrackedGroup)
                .join(TrackedGroup, ScanJob.group_id == TrackedGroup.id)
                .where(ScanJob.id == job_id, ScanJob.status == WorkStatus.RUNNING)
                .with_for_update(of=ScanJob)
            )
            claimed = row.first()
            if claimed is None:
                return ScanProgress(continue_scanning=False, matches_inserted=0)
            job, group = claimed
            if group.status != GroupStatus.ACTIVE:
                job.status = WorkStatus.CANCELLED
                job.completed_at = datetime.now(UTC)
                return ScanProgress(continue_scanning=False, matches_inserted=0)
            inserted = 0
            for match in matches:
                statement = (
                    insert(MessageMatch)
                    .values(
                        group_id=group.id,
                        telegram_message_id=match.telegram_message_id,
                        message_date=match.message_date,
                        source=MatchSource.HISTORY,
                        matched_keywords=match.matched_keywords,
                    )
                    .on_conflict_do_nothing(constraint="uq_message_matches_group_message")
                    .returning(MessageMatch.id)
                )
                match_id = (await session.execute(statement)).scalar_one_or_none()
                if match_id is not None:
                    session.add(NotificationOutbox(match_id=match_id, payload=match.payload))
                    inserted += 1
            job.cursor_message_id = cursor_message_id
            if high_watermark_message_id is not None and (
                job.high_watermark_message_id is None
                or high_watermark_message_id > job.high_watermark_message_id
            ):
                job.high_watermark_message_id = high_watermark_message_id
            job.messages_checked += messages_checked
            job.matches_found += inserted
            return ScanProgress(continue_scanning=True, matches_inserted=inserted)

    async def complete_job(self, job_id: uuid.UUID) -> ScanCompletion | None:
        async with self._session_factory() as session, session.begin():
            row = await session.execute(
                select(ScanJob, TrackedGroup, Owner.notification_chat_id)
                .join(TrackedGroup, ScanJob.group_id == TrackedGroup.id)
                .join(Owner, TrackedGroup.owner_id == Owner.id)
                .where(ScanJob.id == job_id, ScanJob.status == WorkStatus.RUNNING)
                .with_for_update(of=ScanJob)
            )
            claimed = row.first()
            if claimed is None:
                return None
            job, group, notification_chat_id = claimed
            if group.status != GroupStatus.ACTIVE:
                job.status = WorkStatus.CANCELLED
                job.completed_at = datetime.now(UTC)
                return None
            job.status = WorkStatus.COMPLETED
            job.completed_at = datetime.now(UTC)
            if job.high_watermark_message_id is not None and (
                group.last_contiguous_message_id is None
                or job.high_watermark_message_id > group.last_contiguous_message_id
            ):
                group.last_contiguous_message_id = job.high_watermark_message_id
            return ScanCompletion(
                group_title=group.title,
                notification_chat_id=notification_chat_id,
                job_type=job.job_type,
                messages_checked=job.messages_checked,
                matches_found=job.matches_found,
            )

    async def postpone_job(
        self, job_id: uuid.UUID, error: str, *, retry_after: int | None = None
    ) -> None:
        async with self._session_factory() as session, session.begin():
            job = await session.scalar(
                select(ScanJob)
                .where(ScanJob.id == job_id, ScanJob.status == WorkStatus.RUNNING)
                .with_for_update()
            )
            if job is None:
                return
            job.last_error = error[:500]
            if job.attempt_count >= self._MAX_ATTEMPTS:
                job.status = WorkStatus.FAILED
                job.completed_at = datetime.now(UTC)
                return
            delay = retry_after if retry_after is not None else min(2**job.attempt_count, 300)
            job.status = WorkStatus.RETRY
            job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=max(1, delay))

    async def mark_access_lost(self, job_id: uuid.UUID) -> int | None:
        """Pause the group and return a chat ID only for the first loss notification."""
        async with self._session_factory() as session, session.begin():
            row = await session.execute(
                select(ScanJob, TrackedGroup, Owner.notification_chat_id)
                .join(TrackedGroup, ScanJob.group_id == TrackedGroup.id)
                .join(Owner, TrackedGroup.owner_id == Owner.id)
                .where(ScanJob.id == job_id, ScanJob.status == WorkStatus.RUNNING)
                .with_for_update(of=ScanJob)
            )
            claimed = row.first()
            if claimed is None:
                return None
            job, group, notification_chat_id = claimed
            job.status = WorkStatus.CANCELLED
            job.completed_at = datetime.now(UTC)
            if group.status == GroupStatus.REMOVED:
                return None
            group.status = GroupStatus.ACCESS_LOST
            if group.access_lost_notified_at is not None:
                return None
            group.access_lost_notified_at = datetime.now(UTC)
            return notification_chat_id

    async def status_for_owner(self, owner_id: uuid.UUID, limit: int = 12) -> list[ScanJobStatus]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(ScanJob, TrackedGroup.title)
                .join(TrackedGroup, ScanJob.group_id == TrackedGroup.id)
                .where(TrackedGroup.owner_id == owner_id)
                .order_by(ScanJob.created_at.desc(), ScanJob.id.desc())
                .limit(limit)
            )
            return [
                ScanJobStatus(
                    group_title=title,
                    job_type=job.job_type,
                    status=job.status,
                    messages_checked=job.messages_checked,
                    matches_found=job.matches_found,
                    range_end=job.range_end,
                    next_attempt_at=job.next_attempt_at,
                )
                for job, title in rows
            ]


class NotificationRepository:
    """Transactional Postgres outbox operations for Bot API notifications."""

    _MAX_ATTEMPTS = 5

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def requeue_running(self) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(NotificationOutbox)
                .where(NotificationOutbox.status == WorkStatus.RUNNING)
                .values(status=WorkStatus.RETRY, next_attempt_at=datetime.now(UTC))
            )

    async def claim_batch(self, limit: int = 20) -> list[Delivery]:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            rows = await session.execute(
                select(NotificationOutbox, Owner.notification_chat_id)
                .join(MessageMatch, NotificationOutbox.match_id == MessageMatch.id)
                .join(TrackedGroup, MessageMatch.group_id == TrackedGroup.id)
                .join(Owner, TrackedGroup.owner_id == Owner.id)
                .where(
                    NotificationOutbox.status.in_([WorkStatus.PENDING, WorkStatus.RETRY]),
                    or_(
                        NotificationOutbox.next_attempt_at.is_(None),
                        NotificationOutbox.next_attempt_at <= now,
                    ),
                    TrackedGroup.status == GroupStatus.ACTIVE,
                    Owner.notification_chat_id.is_not(None),
                )
                .order_by(NotificationOutbox.created_at, NotificationOutbox.id)
                .limit(limit)
                .with_for_update(skip_locked=True, of=NotificationOutbox)
            )
            deliveries: list[Delivery] = []
            for outbox, notification_chat_id in rows:
                outbox.status = WorkStatus.RUNNING
                outbox.attempt_count += 1
                outbox.next_attempt_at = None
                deliveries.append(
                    Delivery(
                        id=outbox.id,
                        notification_chat_id=notification_chat_id,
                        payload=outbox.payload,
                    )
                )
            return deliveries

    async def mark_sent(self, outbox_id: uuid.UUID, telegram_message_id: int) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.id == outbox_id,
                    NotificationOutbox.status == WorkStatus.RUNNING,
                )
                .values(
                    status=WorkStatus.COMPLETED,
                    telegram_notification_message_id=telegram_message_id,
                    sent_at=datetime.now(UTC),
                    last_error=None,
                )
            )

    async def mark_failed(
        self, outbox_id: uuid.UUID, error: str, *, retry_after: int | None = None
    ) -> None:
        async with self._session_factory() as session, session.begin():
            outbox = await session.scalar(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.id == outbox_id,
                    NotificationOutbox.status == WorkStatus.RUNNING,
                )
                .with_for_update()
            )
            if outbox is None:
                return
            safe_error = error[:500]
            if outbox.attempt_count >= self._MAX_ATTEMPTS:
                outbox.status = WorkStatus.FAILED
                outbox.last_error = safe_error
                return
            delay = retry_after if retry_after is not None else min(2**outbox.attempt_count, 300)
            outbox.status = WorkStatus.RETRY
            outbox.next_attempt_at = datetime.now(UTC) + timedelta(seconds=max(1, delay))
            outbox.last_error = safe_error


async def _get_active_group_for_update(
    session: AsyncSession, owner_id: uuid.UUID, group_id: uuid.UUID
) -> TrackedGroup | None:
    return await session.scalar(
        select(TrackedGroup)
        .where(
            TrackedGroup.id == group_id,
            TrackedGroup.owner_id == owner_id,
            TrackedGroup.status != GroupStatus.REMOVED,
        )
        .with_for_update()
    )


async def _load_card(session: AsyncSession, group: TrackedGroup) -> GroupCard:
    keywords = await session.scalars(
        select(Keyword).where(Keyword.group_id == group.id).order_by(Keyword.created_at, Keyword.id)
    )
    return GroupCard(
        id=group.id,
        title=group.title,
        telegram_chat_id=group.telegram_chat_id,
        username=group.username,
        status=group.status,
        keywords=[KeywordSummary(id=keyword.id, value=keyword.value) for keyword in keywords],
    )


async def _keyword_snapshot(session: AsyncSession, group_id: uuid.UUID) -> list[str]:
    keywords = await session.scalars(
        select(Keyword.normalized_value)
        .where(Keyword.group_id == group_id)
        .order_by(Keyword.created_at, Keyword.id)
    )
    return list(keywords)


def _make_keyword_models(group_id: uuid.UUID, keywords: list[KeywordInput]) -> list[Keyword]:
    return [
        Keyword(group_id=group_id, value=keyword.value, normalized_value=keyword.normalized_value)
        for keyword in keywords
    ]
