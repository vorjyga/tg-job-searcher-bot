"""Transactional notification outbox persistence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.db.models import (
    GroupStatus,
    MessageMatch,
    NotificationOutbox,
    Owner,
    TrackedGroup,
    WorkStatus,
)
from tg_jobs_searcher.db.repository_types import (
    Delivery,
)


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
                    Owner.is_enabled.is_(True),
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

    async def is_deliverable(self, delivery: Delivery) -> bool:
        """Recheck access after a batch claim so revocation stops queued sends."""
        async with self._session_factory() as session:
            outbox_id = await session.scalar(
                select(NotificationOutbox.id)
                .join(MessageMatch, NotificationOutbox.match_id == MessageMatch.id)
                .join(TrackedGroup, MessageMatch.group_id == TrackedGroup.id)
                .join(Owner, TrackedGroup.owner_id == Owner.id)
                .where(
                    NotificationOutbox.id == delivery.id,
                    NotificationOutbox.status == WorkStatus.RUNNING,
                    TrackedGroup.status == GroupStatus.ACTIVE,
                    Owner.is_enabled.is_(True),
                    Owner.notification_chat_id == delivery.notification_chat_id,
                )
            )
            return outbox_id is not None

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
        self,
        outbox_id: uuid.UUID,
        error: str,
        *,
        retry_after: int | None = None,
        permanent: bool = False,
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
            if permanent or outbox.attempt_count >= self._MAX_ATTEMPTS:
                outbox.status = WorkStatus.FAILED
                outbox.last_error = safe_error
                return
            delay = retry_after if retry_after is not None else min(2**outbox.attempt_count, 300)
            outbox.status = WorkStatus.RETRY
            outbox.next_attempt_at = datetime.now(UTC) + timedelta(seconds=max(1, delay))
            outbox.last_error = safe_error
