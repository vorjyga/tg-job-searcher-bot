"""Live monitoring configuration and match persistence."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.db.models import (
    GroupStatus,
    Keyword,
    MatchSource,
    MessageMatch,
    NotificationOutbox,
    Owner,
    TrackedGroup,
)
from tg_jobs_searcher.db.repository_types import (
    MonitoredGroup,
)
from tg_jobs_searcher.services.matching import MatchableKeyword


class MonitoringRepository:
    """Read current group configuration and persist a live match atomically."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def active_groups_for_chat(
        self, telegram_chat_id: int, message_date: datetime
    ) -> list[MonitoredGroup]:
        async with self._session_factory() as session:
            groups = (
                await session.scalars(
                    select(TrackedGroup)
                    .join(Owner, TrackedGroup.owner_id == Owner.id)
                    .where(
                        TrackedGroup.telegram_chat_id == telegram_chat_id,
                        TrackedGroup.status == GroupStatus.ACTIVE,
                        TrackedGroup.monitoring_started_at.is_not(None),
                        TrackedGroup.monitoring_started_at <= message_date,
                        Owner.is_enabled.is_(True),
                    )
                    .order_by(TrackedGroup.id)
                )
            ).all()
            if not groups:
                return []
            keywords = await session.scalars(
                select(Keyword)
                .where(Keyword.group_id.in_([group.id for group in groups]))
                .order_by(Keyword.created_at, Keyword.id)
            )
            keywords_by_group: dict[uuid.UUID, list[MatchableKeyword]] = {
                group.id: [] for group in groups
            }
            for keyword in keywords:
                keywords_by_group[keyword.group_id].append(
                    MatchableKeyword(
                        value=keyword.value,
                        normalized_value=keyword.normalized_value,
                        terms=tuple(keyword.terms),
                    )
                )
            return [
                MonitoredGroup(
                    id=group.id,
                    telegram_chat_id=group.telegram_chat_id,
                    title=group.title,
                    username=group.username,
                    topic_id=group.topic_id,
                    keywords=keywords_by_group[group.id],
                )
                for group in groups
            ]

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
                .join(Owner, TrackedGroup.owner_id == Owner.id)
                .where(
                    TrackedGroup.id == group_id,
                    TrackedGroup.status == GroupStatus.ACTIVE,
                    TrackedGroup.monitoring_started_at.is_not(None),
                    TrackedGroup.monitoring_started_at <= message_date,
                    Owner.is_enabled.is_(True),
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
