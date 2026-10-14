"""Shared notification identity across personal groups and the source catalog."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tg_jobs_searcher.db.models import MatchSource, MessageMatch, NotificationOutbox, WorkStatus


async def persist_match(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    telegram_chat_id: int,
    telegram_message_id: int,
    message_date: datetime,
    matched_keywords: list[str],
    payload: str,
    source: MatchSource,
    group_id: uuid.UUID | None = None,
    catalog_source_id: uuid.UUID | None = None,
) -> bool:
    """Keep both matching routes but enqueue only one notification for a post."""
    match_id = await session.scalar(
        insert(MessageMatch)
        .values(
            owner_id=owner_id,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            message_date=message_date,
            matched_keywords=matched_keywords,
            source=source,
            group_id=group_id,
            catalog_source_id=catalog_source_id,
        )
        .on_conflict_do_nothing()
        .returning(MessageMatch.id)
    )
    if match_id is not None:
        session.add(NotificationOutbox(match_id=match_id, payload=payload))
        return True
    # A match can still be delivered through its other route after one is stopped.
    values = {}
    if group_id is not None:
        values["group_id"] = func.coalesce(MessageMatch.group_id, group_id)
    if catalog_source_id is not None:
        values["catalog_source_id"] = catalog_source_id
    if values:
        await session.execute(
            update(MessageMatch)
            .where(
                MessageMatch.owner_id == owner_id,
                MessageMatch.telegram_chat_id == telegram_chat_id,
                MessageMatch.telegram_message_id == telegram_message_id,
            )
            .values(**values)
        )
    existing_id = select(MessageMatch.id).where(
        MessageMatch.owner_id == owner_id,
        MessageMatch.telegram_chat_id == telegram_chat_id,
        MessageMatch.telegram_message_id == telegram_message_id,
    )
    revived = await session.scalar(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.match_id.in_(existing_id),
            NotificationOutbox.status == WorkStatus.CANCELLED,
        )
        .values(
            status=WorkStatus.PENDING,
            attempt_count=0,
            next_attempt_at=None,
            last_error=None,
            payload=payload,
        )
        .returning(NotificationOutbox.id)
    )
    return revived is not None


def catalog_route_active():
    from tg_jobs_searcher.db.models import CatalogSearch, CatalogSource

    return (
        select(CatalogSearch.owner_id)
        .join(CatalogSource, CatalogSource.id == MessageMatch.catalog_source_id)
        .where(
            CatalogSearch.owner_id == MessageMatch.owner_id,
            CatalogSearch.is_enabled.is_(True),
            CatalogSource.is_enabled.is_(True),
            CatalogSource.monitoring_started_at <= MessageMatch.message_date,
        )
        .exists()
    )
