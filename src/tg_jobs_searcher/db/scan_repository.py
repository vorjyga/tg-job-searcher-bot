"""Historical scan queue and checkpoint persistence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.db.group_repository import _get_active_group_for_update, _keyword_snapshot
from tg_jobs_searcher.db.models import (
    GroupStatus,
    MatchSource,
    MessageMatch,
    NotificationOutbox,
    Owner,
    ScanJob,
    ScanJobType,
    TrackedGroup,
    WorkStatus,
)
from tg_jobs_searcher.db.repository_types import (
    ScanCompletion,
    ScanJobLease,
    ScanJobStatus,
    ScanMatch,
    ScanProgress,
)


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
                .join(Owner, TrackedGroup.owner_id == Owner.id)
                .where(
                    TrackedGroup.status == GroupStatus.ACTIVE,
                    TrackedGroup.monitoring_started_at.is_not(None),
                    Owner.is_enabled.is_(True),
                )
                .with_for_update()
            )
            for group in groups:
                active_job = await session.scalar(
                    select(ScanJob.id).where(
                        ScanJob.group_id == group.id,
                        ScanJob.status.in_(
                            [
                                WorkStatus.PENDING,
                                WorkStatus.RUNNING,
                                WorkStatus.RETRY,
                            ]
                        ),
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
                    Owner.is_enabled.is_(True),
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
                topic_id=group.topic_id,
                notification_chat_id=notification_chat_id,
                job_type=job.job_type,
                range_start=job.range_start,
                range_end=job.range_end,
                keyword_snapshot=[
                    value for value in job.keyword_snapshot if isinstance(value, (str, dict))
                ],
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
                        source=(
                            MatchSource.RECOVERY
                            if job.job_type == ScanJobType.RECOVERY
                            else MatchSource.HISTORY
                        ),
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
