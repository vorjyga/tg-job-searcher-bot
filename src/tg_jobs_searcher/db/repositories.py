"""PostgreSQL repositories for owners, dialogs and tracked-group configuration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.db.models import (
    ConversationState,
    GroupStatus,
    Keyword,
    MessageMatch,
    NotificationOutbox,
    Owner,
    ScanJob,
    ScanJobType,
    TrackedGroup,
    WorkStatus,
)
from tg_jobs_searcher.services.keywords import KeywordInput
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


def _make_keyword_models(group_id: uuid.UUID, keywords: list[KeywordInput]) -> list[Keyword]:
    return [
        Keyword(group_id=group_id, value=keyword.value, normalized_value=keyword.normalized_value)
        for keyword in keywords
    ]
