"""Tracked group configuration persistence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.db.match_repository import catalog_route_active
from tg_jobs_searcher.db.models import (
    AnalyticsEvent,
    AnalyticsEventType,
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
from tg_jobs_searcher.db.repository_types import (
    GroupCard,
    GroupSummary,
    KeywordSummary,
)
from tg_jobs_searcher.services.keywords import (
    MAX_KEYWORDS_PER_USER,
    KeywordInput,
    KeywordInputError,
)
from tg_jobs_searcher.telegram.client import ResolvedGroup


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
        keywords: list[KeywordInput] | None = None,
        *,
        scan_history: bool,
    ) -> GroupCard:
        async with self._session_factory() as session, session.begin():
            await _lock_owner(session, owner_id)
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
                    topic_id=resolved_group.topic_id,
                    topic_title=resolved_group.topic_title,
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
                group.topic_id = resolved_group.topic_id
                group.topic_title = resolved_group.topic_title
                group.status = GroupStatus.ACTIVE
                group.deleted_at = None
                group.monitoring_started_at = datetime.now(UTC)
                group.configuration_version += 1
            else:
                raise ValueError("This group is already tracked")
            shared_keywords = await _keyword_snapshot(session, owner_id)
            if not shared_keywords:
                if not keywords:
                    raise KeywordInputError("Сначала добавьте общие условия поиска")
                session.add_all(_make_keyword_models(owner_id, keywords))
                await _update_keyword_groups(session, owner_id, has_keywords=True)
                shared_keywords = [_input_snapshot(keyword) for keyword in keywords]
            await _record_group_event(session, owner_id, group.id, AnalyticsEventType.GROUP_ADDED)
            if scan_history:
                range_end = datetime.now(UTC)
                session.add(
                    ScanJob(
                        group_id=group.id,
                        job_type=ScanJobType.INITIAL_SEVEN_DAYS,
                        range_start=range_end - timedelta(days=7),
                        range_end=range_end,
                        keyword_snapshot=shared_keywords,
                    )
                )
            group_id = group.id
        card = await self.get_card(owner_id, group_id)
        assert card is not None
        return card

    async def get_keywords(self, owner_id: uuid.UUID) -> list[KeywordInput]:
        """Return the current shared rules, including legacy literal conditions."""
        async with self._session_factory() as session:
            keywords = await session.scalars(
                select(Keyword)
                .where(Keyword.owner_id == owner_id)
                .order_by(Keyword.created_at, Keyword.id)
            )
            return [
                KeywordInput(keyword.value, keyword.normalized_value, tuple(keyword.terms))
                for keyword in keywords
            ]

    async def append_keywords(
        self, owner_id: uuid.UUID, group_id: uuid.UUID, keywords: list[KeywordInput]
    ) -> GroupCard | None:
        """Append shared rules from any of the owner's current group cards."""
        async with self._session_factory() as session, session.begin():
            await _lock_owner(session, owner_id)
            group = await _get_active_group_for_update(session, owner_id, group_id)
            if group is None:
                return None
            existing = await session.execute(
                select(Keyword.normalized_value, Keyword.terms).where(Keyword.owner_id == owner_id)
            )
            existing_rows = existing.all()
            seen_terms = {tuple(terms) for _, terms in existing_rows}
            seen_values = {value for value, _ in existing_rows}
            new_keywords = []
            for keyword in keywords:
                if keyword.terms in seen_terms or keyword.normalized_value in seen_values:
                    continue
                seen_terms.add(keyword.terms)
                seen_values.add(keyword.normalized_value)
                new_keywords.append(keyword)
            if new_keywords and len(existing_rows) + len(new_keywords) > MAX_KEYWORDS_PER_USER:
                raise KeywordInputError(
                    f"Для пользователя допускается не более {MAX_KEYWORDS_PER_USER} условий поиска"
                )
            if new_keywords:
                statement = insert(Keyword).values(
                    [
                        {
                            "owner_id": owner_id,
                            "value": keyword.value,
                            "normalized_value": keyword.normalized_value,
                            "terms": list(keyword.terms),
                        }
                        for keyword in new_keywords
                    ]
                )
                statement = statement.on_conflict_do_nothing(
                    constraint="uq_keywords_owner_normalized_value"
                )
                await session.execute(statement)
                await _update_keyword_groups(session, owner_id, has_keywords=True)
        return await self.get_card(owner_id, group_id)

    async def replace_keywords(
        self, owner_id: uuid.UUID, group_id: uuid.UUID, keywords: list[KeywordInput]
    ) -> GroupCard | None:
        async with self._session_factory() as session, session.begin():
            await _lock_owner(session, owner_id)
            group = await _get_active_group_for_update(session, owner_id, group_id)
            if group is None:
                return None
            await session.execute(delete(Keyword).where(Keyword.owner_id == owner_id))
            session.add_all(_make_keyword_models(owner_id, keywords))
            await _update_keyword_groups(session, owner_id, has_keywords=bool(keywords))
        return await self.get_card(owner_id, group_id)

    async def delete_keyword(self, owner_id: uuid.UUID, keyword_id: uuid.UUID) -> GroupCard | None:
        async with self._session_factory() as session, session.begin():
            await _lock_owner(session, owner_id)
            keyword = await session.scalar(
                select(Keyword).where(Keyword.id == keyword_id, Keyword.owner_id == owner_id)
            )
            if keyword is None:
                return None
            group = await session.scalar(
                select(TrackedGroup)
                .where(
                    TrackedGroup.owner_id == owner_id, TrackedGroup.status != GroupStatus.REMOVED
                )
                .order_by(TrackedGroup.id)
                .limit(1)
                .with_for_update()
            )
            if group is None:
                return None
            group_id = group.id
            await session.delete(keyword)
            await session.flush()
            remaining = await session.scalar(
                select(func.count()).select_from(Keyword).where(Keyword.owner_id == owner_id)
            )
            await _update_keyword_groups(session, owner_id, has_keywords=bool(remaining))
        return await self.get_card(owner_id, group_id)

    async def remove_group(self, owner_id: uuid.UUID, group_id: uuid.UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            group = await _get_active_group_for_update(session, owner_id, group_id)
            if group is None:
                return False
            group.status = GroupStatus.REMOVED
            group.deleted_at = datetime.now(UTC)
            group.configuration_version += 1
            await _record_group_event(session, owner_id, group.id, AnalyticsEventType.GROUP_REMOVED)
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
                        select(MessageMatch.id).where(
                            MessageMatch.group_id == group_id, ~catalog_route_active()
                        )
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
            was_access_lost = group.status == GroupStatus.ACCESS_LOST
            group.title = resolved_group.title
            group.username = resolved_group.username
            keywords = await _keyword_snapshot(session, group.owner_id)
            group.status = GroupStatus.ACTIVE if keywords else GroupStatus.PAUSED
            group.access_lost_notified_at = None
            if was_access_lost and keywords and group.monitoring_started_at is not None:
                now = datetime.now(UTC)
                session.add(
                    ScanJob(
                        group_id=group.id,
                        job_type=ScanJobType.RECOVERY,
                        range_start=max(now - timedelta(days=7), group.monitoring_started_at),
                        range_end=now,
                        keyword_snapshot=keywords,
                    )
                )
        return await self.get_card(owner_id, group_id)


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
        select(Keyword)
        .where(Keyword.owner_id == group.owner_id)
        .order_by(Keyword.created_at, Keyword.id)
    )
    return GroupCard(
        id=group.id,
        title=group.title,
        telegram_chat_id=group.telegram_chat_id,
        username=group.username,
        topic_id=group.topic_id,
        topic_title=group.topic_title,
        status=group.status,
        keywords=[KeywordSummary(id=keyword.id, value=keyword.value) for keyword in keywords],
    )


async def _record_group_event(
    session: AsyncSession,
    owner_id: uuid.UUID,
    group_id: uuid.UUID,
    event_type: AnalyticsEventType,
) -> None:
    telegram_user_id = await session.scalar(
        select(Owner.telegram_user_id).where(Owner.id == owner_id)
    )
    assert telegram_user_id is not None
    session.add(
        AnalyticsEvent(
            event_type=event_type.value,
            telegram_user_id=telegram_user_id,
            group_id=group_id,
            occurred_at=datetime.now(UTC),
        )
    )


async def _lock_owner(session: AsyncSession, owner_id: uuid.UUID) -> None:
    # Serialise all shared-list writes, including initial setup for a new group.
    await session.scalar(select(Owner.id).where(Owner.id == owner_id).with_for_update())


async def _update_keyword_groups(
    session: AsyncSession, owner_id: uuid.UUID, *, has_keywords: bool
) -> None:
    await session.execute(
        update(TrackedGroup)
        .where(TrackedGroup.owner_id == owner_id, TrackedGroup.status != GroupStatus.REMOVED)
        .values(configuration_version=TrackedGroup.configuration_version + 1)
    )
    # Access-lost groups stay inaccessible until their access has been checked.
    await session.execute(
        update(TrackedGroup)
        .where(
            TrackedGroup.owner_id == owner_id,
            TrackedGroup.status == (GroupStatus.PAUSED if has_keywords else GroupStatus.ACTIVE),
        )
        .values(status=GroupStatus.ACTIVE if has_keywords else GroupStatus.PAUSED)
    )


async def _keyword_snapshot(session: AsyncSession, owner_id: uuid.UUID) -> list[dict[str, Any]]:
    keywords = await session.scalars(
        select(Keyword).where(Keyword.owner_id == owner_id).order_by(Keyword.created_at, Keyword.id)
    )
    return [_stored_keyword_snapshot(keyword) for keyword in keywords]


def _input_snapshot(keyword: KeywordInput) -> dict[str, Any]:
    return {"value": keyword.value, "terms": list(keyword.terms)}


def _stored_keyword_snapshot(keyword: Keyword) -> dict[str, Any]:
    return {"value": keyword.value, "terms": keyword.terms}


def _make_keyword_models(owner_id: uuid.UUID, keywords: list[KeywordInput]) -> list[Keyword]:
    return [
        Keyword(
            owner_id=owner_id,
            value=keyword.value,
            normalized_value=keyword.normalized_value,
            terms=list(keyword.terms),
        )
        for keyword in keywords
    ]
