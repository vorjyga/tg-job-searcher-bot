"""User access and onboarding persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.db.group_repository import _keyword_snapshot
from tg_jobs_searcher.db.models import (
    AccessMode,
    AnalyticsEvent,
    AnalyticsEventType,
    BotAccessSettings,
    GroupStatus,
    MessageMatch,
    NotificationOutbox,
    Owner,
    ScanJob,
    ScanJobType,
    TrackedGroup,
    WorkStatus,
)


async def _access_mode(session: AsyncSession) -> AccessMode:
    mode = await session.scalar(
        select(BotAccessSettings.access_mode).where(BotAccessSettings.id == 1)
    )
    if mode is None:
        raise RuntimeError("Bot access settings are missing; run database migrations")
    return AccessMode(mode)


class OwnerRepository:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], admin_telegram_id: int
    ) -> None:
        self._session_factory = session_factory
        self._admin_telegram_id = admin_telegram_id

    async def is_authorized(self, telegram_user_id: int) -> bool:
        if telegram_user_id == self._admin_telegram_id:
            return True
        async with self._session_factory() as session:
            enabled = await session.scalar(
                select(Owner.is_enabled).where(Owner.telegram_user_id == telegram_user_id)
            )
            if enabled is not None:
                return enabled
            return await _access_mode(session) == AccessMode.OPEN

    async def is_disabled(self, telegram_user_id: int) -> bool:
        async with self._session_factory() as session:
            return (
                await session.scalar(
                    select(Owner.is_enabled).where(Owner.telegram_user_id == telegram_user_id)
                )
                is False
            )

    async def get_access_mode(self) -> AccessMode:
        async with self._session_factory() as session:
            return await _access_mode(session)

    async def set_access_mode(self, mode: AccessMode) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(BotAccessSettings)
                .where(BotAccessSettings.id == 1)
                .values(access_mode=mode.value)
            )

    async def ensure_owner(self, telegram_user_id: int, notification_chat_id: int) -> Owner:
        async with self._session_factory() as session, session.begin():
            existing = await session.scalar(
                select(Owner.id).where(Owner.telegram_user_id == telegram_user_id)
            )
            if (
                existing is None
                and telegram_user_id != self._admin_telegram_id
                and await _access_mode(session) == AccessMode.INVITE
            ):
                raise PermissionError("Telegram user needs an invitation")
            await session.execute(
                insert(Owner)
                .values(telegram_user_id=telegram_user_id, is_enabled=True)
                .on_conflict_do_nothing(index_elements=[Owner.telegram_user_id])
            )
            owner = await session.scalar(
                select(Owner).where(Owner.telegram_user_id == telegram_user_id).with_for_update()
            )
            assert owner is not None
            if telegram_user_id == self._admin_telegram_id:
                owner.is_enabled = True
            if not owner.is_enabled:
                raise PermissionError("Telegram user is disabled by the administrator")
            owner.notification_chat_id = notification_chat_id
            owner.is_bot_blocked = False
            return owner

    async def start_user(
        self, telegram_user_id: int, notification_chat_id: int
    ) -> tuple[Owner | None, bool]:
        """Register a private-chat /start and return whether it is this user's first one."""
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            existing = await session.scalar(
                select(Owner.id).where(Owner.telegram_user_id == telegram_user_id)
            )
            if (
                existing is None
                and telegram_user_id != self._admin_telegram_id
                and await _access_mode(session) == AccessMode.INVITE
            ):
                return None, False
            await session.execute(
                insert(Owner)
                .values(telegram_user_id=telegram_user_id, is_enabled=True)
                .on_conflict_do_nothing(index_elements=[Owner.telegram_user_id])
            )
            owner = await session.scalar(
                select(Owner).where(Owner.telegram_user_id == telegram_user_id).with_for_update()
            )
            assert owner is not None
            if telegram_user_id == self._admin_telegram_id:
                owner.is_enabled = True
            if not owner.is_enabled and telegram_user_id != self._admin_telegram_id:
                return None, False
            first_start = owner.started_at is None
            if first_start:
                owner.started_at = now
                if telegram_user_id != self._admin_telegram_id:
                    session.add(
                        AnalyticsEvent(
                            event_type=AnalyticsEventType.USER_JOINED.value,
                            telegram_user_id=telegram_user_id,
                            occurred_at=now,
                        )
                    )
            owner.notification_chat_id = notification_chat_id
            owner.is_bot_blocked = False
            return owner, first_start and telegram_user_id != self._admin_telegram_id

    async def record_bot_block_state(
        self, telegram_user_id: int, *, blocked: bool, occurred_at: datetime
    ) -> None:
        if telegram_user_id == self._admin_telegram_id:
            return
        async with self._session_factory() as session, session.begin():
            owner = await session.scalar(
                select(Owner).where(Owner.telegram_user_id == telegram_user_id).with_for_update()
            )
            if owner is None or owner.is_bot_blocked == blocked:
                return
            owner.is_bot_blocked = blocked
            owner.notification_chat_id = None if blocked else telegram_user_id
            if blocked and owner.started_at is not None:
                session.add(
                    AnalyticsEvent(
                        event_type=AnalyticsEventType.BOT_BLOCKED.value,
                        telegram_user_id=telegram_user_id,
                        occurred_at=occurred_at,
                    )
                )

    async def grant_user(self, telegram_user_id: int) -> bool:
        """Grant access; return whether this is a new or restored grant."""
        if telegram_user_id == self._admin_telegram_id:
            return False
        async with self._session_factory() as session, session.begin():
            inserted_id = await session.scalar(
                insert(Owner)
                .values(telegram_user_id=telegram_user_id, is_enabled=True)
                .on_conflict_do_nothing(index_elements=[Owner.telegram_user_id])
                .returning(Owner.id)
            )
            if inserted_id is not None:
                return True
            owner = await session.scalar(
                select(Owner).where(Owner.telegram_user_id == telegram_user_id).with_for_update()
            )
            assert owner is not None
            if owner.is_enabled:
                return False
            owner.is_enabled = True
            owner.notification_chat_id = None
            now = datetime.now(UTC)
            groups = await session.scalars(
                select(TrackedGroup).where(
                    TrackedGroup.owner_id == owner.id,
                    TrackedGroup.status == GroupStatus.ACTIVE,
                    TrackedGroup.monitoring_started_at.is_not(None),
                )
            )
            for group in groups:
                session.add(
                    ScanJob(
                        group_id=group.id,
                        job_type=ScanJobType.RECOVERY,
                        range_start=max(now - timedelta(days=7), group.monitoring_started_at),
                        range_end=now,
                        keyword_snapshot=await _keyword_snapshot(session, group.id),
                    )
                )
            return True

    async def revoke_user(self, telegram_user_id: int) -> bool:
        if telegram_user_id == self._admin_telegram_id:
            return False
        async with self._session_factory() as session, session.begin():
            owner = await session.scalar(
                select(Owner)
                .where(Owner.telegram_user_id == telegram_user_id, Owner.is_enabled.is_(True))
                .with_for_update()
            )
            if owner is None:
                return False
            owner.is_enabled = False
            owner.notification_chat_id = None
            group_ids = select(TrackedGroup.id).where(TrackedGroup.owner_id == owner.id)
            await session.execute(
                update(ScanJob)
                .where(
                    ScanJob.group_id.in_(group_ids),
                    ScanJob.status.in_([WorkStatus.PENDING, WorkStatus.RUNNING, WorkStatus.RETRY]),
                )
                .values(status=WorkStatus.CANCELLED, completed_at=datetime.now(UTC))
            )
            await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.match_id.in_(
                        select(MessageMatch.id).where(MessageMatch.group_id.in_(group_ids))
                    ),
                    NotificationOutbox.status.in_(
                        [WorkStatus.PENDING, WorkStatus.RUNNING, WorkStatus.RETRY]
                    ),
                )
                .values(status=WorkStatus.CANCELLED)
            )
            return True

    async def list_users(self) -> list[tuple[int, bool]]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(Owner.telegram_user_id, Owner.is_enabled)
                .where(Owner.telegram_user_id != self._admin_telegram_id)
                .order_by(Owner.telegram_user_id)
            )
            return [(telegram_user_id, is_enabled) for telegram_user_id, is_enabled in rows]
