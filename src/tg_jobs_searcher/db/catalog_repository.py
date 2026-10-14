"""Administrator source catalog, import queue and personal catalog searches."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.db.match_repository import persist_match
from tg_jobs_searcher.db.models import (
    BotAccessSettings,
    CatalogImport,
    CatalogSearch,
    CatalogSource,
    GroupStatus,
    MatchSource,
    MessageMatch,
    NotificationOutbox,
    Owner,
    TrackedGroup,
    WorkStatus,
)
from tg_jobs_searcher.services.keywords import KeywordInput
from tg_jobs_searcher.services.matching import find_matching_keywords, keywords_from_snapshot
from tg_jobs_searcher.services.notifications import format_notification
from tg_jobs_searcher.telegram.client import ResolvedGroup


class CatalogRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def set_search(self, owner_id: uuid.UUID, keywords: list[KeywordInput]) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            owner = await session.scalar(
                select(Owner)
                .where(Owner.id == owner_id, Owner.is_enabled.is_(True))
                .with_for_update()
            )
            if owner is None:
                raise PermissionError("User disabled")
            # New conditions start now; already queued matches retain their old conditions.
            snapshot = [dict(value=k.value, terms=list(k.terms)) for k in keywords]
            await session.execute(
                insert(CatalogSearch)
                .values(
                    owner_id=owner_id,
                    keywords=snapshot,
                    is_enabled=True,
                    monitoring_started_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[CatalogSearch.owner_id],
                    set_={
                        "keywords": snapshot,
                        "is_enabled": True,
                        "monitoring_started_at": now,
                    },
                )
            )

    async def get_search(self, owner_id: uuid.UUID) -> CatalogSearch | None:
        async with self._session_factory() as session:
            return await session.get(CatalogSearch, owner_id)

    async def stop_search(self, owner_id: uuid.UUID) -> None:
        async with self._session_factory() as session, session.begin():
            await session.scalar(select(Owner).where(Owner.id == owner_id).with_for_update())
            await session.execute(
                update(CatalogSearch)
                .where(CatalogSearch.owner_id == owner_id)
                .values(is_enabled=False)
            )
            await _cancel_unrouted(session, MessageMatch.owner_id == owner_id)

    async def source_count(self) -> int:
        async with self._session_factory() as session:
            return (
                await session.scalar(
                    select(func.count())
                    .select_from(CatalogSource)
                    .where(CatalogSource.is_enabled.is_(True))
                )
                or 0
            )

    async def list_sources(self, page: int = 1, page_size: int = 15) -> list[CatalogSource]:
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(CatalogSource)
                    .order_by(CatalogSource.title, CatalogSource.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )

    async def add_source(self, group: ResolvedGroup) -> None:
        if group.topic_id is not None:
            raise ValueError("В общий каталог добавляется весь чат. Используйте ссылку на группу.")
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            source = await session.scalar(
                select(CatalogSource)
                .where(CatalogSource.telegram_chat_id == group.telegram_chat_id)
                .with_for_update()
            )
            if source is None:
                session.add(
                    CatalogSource(
                        telegram_chat_id=group.telegram_chat_id,
                        title=group.title,
                        username=group.username,
                        monitoring_started_at=now,
                        next_recovery_at=now,
                    )
                )
            else:
                source.title = group.title
                source.username = group.username
                source.last_error = None
                if not source.is_enabled:
                    source.is_enabled = True
                    source.monitoring_started_at = now
                    source.last_contiguous_message_id = None
                source.next_recovery_at = now

    async def remove_source(self, chat_id: int) -> bool:
        async with self._session_factory() as session, session.begin():
            source = await session.scalar(
                select(CatalogSource)
                .where(CatalogSource.telegram_chat_id == chat_id)
                .with_for_update()
            )
            if source is None or not source.is_enabled:
                return False
            source.is_enabled = False
            await _cancel_unrouted(session, MessageMatch.catalog_source_id == source.id)
            return True

    async def process_message(
        self,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
        message_date: datetime,
        text: str,
        source: MatchSource = MatchSource.LIVE,
    ) -> int:
        count = 0
        async with self._session_factory() as session, session.begin():
            catalog_source = await session.scalar(
                select(CatalogSource)
                .where(
                    CatalogSource.telegram_chat_id == telegram_chat_id,
                    CatalogSource.is_enabled.is_(True),
                    CatalogSource.monitoring_started_at <= message_date,
                )
                .with_for_update()
            )
            if catalog_source is None:
                return 0
            searches = list(
                await session.scalars(
                    select(CatalogSearch)
                    .join(Owner, CatalogSearch.owner_id == Owner.id)
                    .where(
                        Owner.is_enabled.is_(True),
                        CatalogSearch.is_enabled.is_(True),
                        CatalogSearch.monitoring_started_at <= message_date,
                    )
                    .order_by(CatalogSearch.owner_id)
                )
            )
            for candidate in searches:
                if not find_matching_keywords(text, keywords_from_snapshot(candidate.keywords)):
                    continue
                owner_id = candidate.owner_id
                # Serialise against disabling users and changing/stopping their search.
                owner = await session.scalar(
                    select(Owner)
                    .where(Owner.id == owner_id, Owner.is_enabled.is_(True))
                    .with_for_update()
                )
                if owner is None:
                    continue
                search = await session.get(CatalogSearch, owner_id, populate_existing=True)
                if (
                    search is None
                    or not search.is_enabled
                    or search.monitoring_started_at > message_date
                ):
                    continue
                matched = find_matching_keywords(text, keywords_from_snapshot(search.keywords))
                if not matched:
                    continue
                count += await persist_match(
                    session,
                    owner_id=owner_id,
                    telegram_chat_id=telegram_chat_id,
                    catalog_source_id=catalog_source.id,
                    telegram_message_id=telegram_message_id,
                    message_date=message_date,
                    matched_keywords=matched,
                    source=source,
                    payload=format_notification(
                        group_title=catalog_source.title,
                        group_username=catalog_source.username,
                        telegram_chat_id=telegram_chat_id,
                        telegram_message_id=telegram_message_id,
                        message_date=message_date,
                        matched_keywords=matched,
                        text=text,
                    ),
                )
        return count

    async def enqueue_imports(self, references: list[str]) -> int:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            count = 0
            for reference in dict.fromkeys(references):
                row = await session.scalar(
                    insert(CatalogImport)
                    .values(
                        reference=reference,
                        next_attempt_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[CatalogImport.reference],
                        set_={
                            "status": "pending",
                            "attempt_count": 0,
                            "next_attempt_at": now,
                            "last_error": None,
                        },
                        where=CatalogImport.status.in_(["failed", "completed"]),
                    )
                    .returning(CatalogImport.id)
                )
                count += row is not None
            return count

    async def import_status(self) -> list[tuple[str, int]]:
        async with self._session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(CatalogImport.status, func.count())
                        .group_by(CatalogImport.status)
                        .order_by(CatalogImport.status)
                    )
                ).all()
            )

    async def failed_imports(self, page: int = 1) -> list[CatalogImport]:
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(CatalogImport)
                    .where(CatalogImport.status == "failed")
                    .order_by(CatalogImport.created_at, CatalogImport.id)
                    .offset((page - 1) * 10)
                    .limit(10)
                )
            )

    async def requeue_imports(self) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(CatalogImport)
                .where(CatalogImport.status == "running")
                .values(status="pending")
            )

    async def claim_import(self) -> CatalogImport | None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            retry_at = await session.scalar(
                select(BotAccessSettings.catalog_join_retry_at).where(
                    BotAccessSettings.id == 1,
                )
            )
            if retry_at is not None and retry_at > now:
                return None
            job = await session.scalar(
                select(CatalogImport)
                .where(
                    CatalogImport.status == "pending",
                    CatalogImport.next_attempt_at <= now,
                )
                .order_by(CatalogImport.created_at, CatalogImport.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is not None:
                job.status = "running"
                job.attempt_count += 1
            return job

    async def finish_import(
        self, job_id: uuid.UUID, *, error: str | None = None, retry_after: int | None = None
    ) -> None:
        async with self._session_factory() as session, session.begin():
            job = await session.get(CatalogImport, job_id, with_for_update=True)
            if job is None:
                return
            job.last_error = error[:500] if error else None
            job.status = "completed" if error is None else "failed"
            if retry_after is not None:
                job.status = "pending"
                job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=max(1, retry_after))
                # Persist the account cooldown separately: newly submitted links
                # must not bypass a FloodWait already imposed on this queue.
                await session.execute(
                    update(BotAccessSettings)
                    .where(
                        BotAccessSettings.id == 1,
                    )
                    .values(catalog_join_retry_at=job.next_attempt_at)
                )
                # Telegram limits apply to the shared account, not only this source.
                await session.execute(
                    update(CatalogImport)
                    .where(CatalogImport.status == "pending")
                    .values(next_attempt_at=job.next_attempt_at)
                )

    async def due_sources(self) -> list[CatalogSource]:
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(CatalogSource)
                    .where(
                        CatalogSource.is_enabled.is_(True),
                        CatalogSource.next_recovery_at <= datetime.now(UTC),
                    )
                    .order_by(CatalogSource.next_recovery_at, CatalogSource.id)
                    .limit(20)
                )
            )

    async def recovery_progress(
        self,
        source_id: uuid.UUID,
        started_at: datetime,
        cursor: int | None,
        *,
        error: str | None = None,
        delay: int = 300,
        access_lost: bool = False,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            source = await session.get(CatalogSource, source_id, with_for_update=True)
            if (
                source is None
                or not source.is_enabled
                or source.monitoring_started_at != started_at
            ):
                return
            if cursor is not None:
                source.last_contiguous_message_id = max(
                    source.last_contiguous_message_id or 0, cursor
                )
            source.next_recovery_at = datetime.now(UTC) + timedelta(seconds=delay)
            source.last_error = error
            if access_lost:
                source.is_enabled = False
                await _cancel_unrouted(session, MessageMatch.catalog_source_id == source.id)


async def _cancel_unrouted(session: AsyncSession, condition) -> None:
    from tg_jobs_searcher.db.match_repository import catalog_route_active

    personal_active = (
        select(TrackedGroup.id)
        .where(
            TrackedGroup.id == MessageMatch.group_id,
            TrackedGroup.status == GroupStatus.ACTIVE,
        )
        .exists()
    )
    await session.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.match_id.in_(
                select(MessageMatch.id).where(
                    condition,
                    ~personal_active,
                    ~catalog_route_active(),
                )
            ),
            NotificationOutbox.status.in_(
                [WorkStatus.PENDING, WorkStatus.RUNNING, WorkStatus.RETRY]
            ),
        )
        .values(status=WorkStatus.CANCELLED)
    )
