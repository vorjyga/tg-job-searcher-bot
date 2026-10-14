"""Durable source imports and bounded catch-up for the shared catalog."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from telethon import TelegramClient, errors

from tg_jobs_searcher.bot.handlers import GroupResolver
from tg_jobs_searcher.db.catalog_repository import CatalogRepository
from tg_jobs_searcher.db.models import CatalogSource, MatchSource
from tg_jobs_searcher.services.pause import PauseCoordinator
from tg_jobs_searcher.services.scanning import _ACCESS_LOST_ERRORS, PAGE_SIZE, _wait_for_stop
from tg_jobs_searcher.telegram.client import GroupResolutionError

logger = logging.getLogger(__name__)


def flood_wait_seconds(exc: BaseException) -> int | None:
    """The existing join helper wraps RPC errors, retaining their cause."""
    while exc is not None:
        if isinstance(exc, errors.FloodWaitError):
            return int(exc.seconds)
        exc = exc.__cause__
    return None


class CatalogImportWorker:
    def __init__(
        self,
        repository: CatalogRepository,
        resolve_group: GroupResolver,
        pause: PauseCoordinator,
        interval: float = 5,
    ) -> None:
        self._repository = repository
        self._resolve_group = resolve_group
        self._pause = pause
        self._interval = interval

    async def run(self, stop_event: asyncio.Event) -> None:
        recover_running = True
        while not stop_event.is_set():
            try:
                if recover_running:
                    await self._repository.requeue_imports()
                    recover_running = False
                async with self._pause.activity() as allowed:
                    if allowed:
                        job = await self._repository.claim_import()
                        if job is not None:
                            await self._import(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("catalog_import_worker_failed")
                recover_running = True
            await _wait_for_stop(stop_event, self._interval)

    async def _import(self, job) -> None:
        try:
            group = await self._resolve_group(job.reference)
            await self._repository.add_source(group)
        except (GroupResolutionError, errors.RPCError, ValueError) as exc:
            delay = flood_wait_seconds(exc)
            # Never persist raw Telegram request data or account credentials.
            message = (
                str(exc).replace("/add", "/sources_add")
                if isinstance(exc, (GroupResolutionError, ValueError))
                else "Telegram RPC error"
            )
            await self._repository.finish_import(job.id, error=message, retry_after=delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("catalog_source_import_failed")
            await self._repository.finish_import(
                job.id,
                error="Временная ошибка импорта",
                retry_after=60 if job.attempt_count < 5 else None,
            )
        else:
            await self._repository.finish_import(job.id)


class CatalogRecoveryWorker:
    """Read each source once, checkpoint only after durable message processing.

    Live events never advance this cursor. Catch-up reads oldest first and commits
    one page per source per cycle so a busy source cannot starve the rest.
    """

    def __init__(self, repository: CatalogRepository, client: TelegramClient) -> None:
        self._repository = repository
        self._client = client
        self._peers: dict[int, object] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                sources = await self._repository.due_sources()
                for source in sources:
                    if stop_event.is_set():
                        return
                    delay = await self._recover(source)
                    if delay is not None:
                        # FloodWait is an account-wide limit; defer every source.
                        await _wait_for_stop(stop_event, delay)
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("catalog_recovery_worker_failed")
            await _wait_for_stop(stop_event, 2)

    async def _entity(self, chat_id: int):
        if chat_id in self._peers:
            return self._peers[chat_id]
        try:
            peer = await self._client.get_input_entity(chat_id)
        except ValueError:
            async for dialog in self._client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    self._peers[dialog.id] = dialog.input_entity
            if chat_id not in self._peers:
                raise ValueError("Источник отсутствует среди доступных чатов аккаунта") from None
            peer = self._peers[chat_id]
        self._peers[chat_id] = peer
        return peer

    async def _recover(self, source: CatalogSource) -> int | None:
        try:
            entity = await self._entity(source.telegram_chat_id)
            boundary = max(source.monitoring_started_at, datetime.now(UTC) - timedelta(days=7))
            # Telethon gives an ID offset priority over a date offset. Find the
            # lower ID boundary explicitly so a cursor older than seven days
            # cannot turn catch-up into an unbounded history scan.
            before = [
                message
                async for message in self._client.iter_messages(
                    entity,
                    offset_date=boundary,
                    limit=1,
                )
            ]
            min_id = max(source.last_contiguous_message_id or 0, before[0].id if before else 0)
            page = [
                message
                async for message in self._client.iter_messages(
                    entity,
                    reverse=True,
                    min_id=min_id,
                    offset_date=boundary if not min_id else None,
                    limit=PAGE_SIZE,
                )
            ]
            cursor = min_id or source.last_contiguous_message_id
            for message in page:
                message_date = message.date
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=UTC)
                if message_date >= boundary and message.raw_text:
                    await self._repository.process_message(
                        telegram_chat_id=source.telegram_chat_id,
                        telegram_message_id=message.id,
                        message_date=message_date,
                        text=message.raw_text,
                        source=MatchSource.RECOVERY,
                    )
                cursor = max(cursor or 0, message.id)
            await self._repository.recovery_progress(
                source.id,
                source.monitoring_started_at,
                cursor,
                delay=0 if len(page) == PAGE_SIZE else 300,
            )
        except asyncio.CancelledError:
            raise
        except errors.FloodWaitError as exc:
            await self._repository.recovery_progress(
                source.id,
                source.monitoring_started_at,
                None,
                error="Telegram ограничил чтение; повторная попытка запланирована.",
                delay=exc.seconds,
            )
            return exc.seconds
        except (*_ACCESS_LOST_ERRORS, ValueError):
            self._peers.pop(source.telegram_chat_id, None)
            await self._repository.recovery_progress(
                source.id,
                source.monitoring_started_at,
                None,
                access_lost=True,
                error="Доступ потерян. Восстановите членство и повторите /sources_add.",
            )
        except Exception:
            logger.exception("catalog_source_recovery_failed")
            await self._repository.recovery_progress(
                source.id,
                source.monitoring_started_at,
                None,
                error="Временная ошибка чтения; повторная попытка запланирована.",
                delay=60,
            )
        return None
