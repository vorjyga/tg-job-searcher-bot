"""Background Telethon scans for initial history, manual reruns and recovery."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC

from aiogram import Bot
from telethon import TelegramClient, errors

from tg_jobs_searcher.db.models import ScanJobType
from tg_jobs_searcher.db.repositories import (
    ScanCompletion,
    ScanJobLease,
    ScanMatch,
    ScanRepository,
)
from tg_jobs_searcher.services.matching import MatchableKeyword, find_matching_keywords
from tg_jobs_searcher.services.notifications import format_notification
from tg_jobs_searcher.telegram.topics import message_in_topic

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
_ACCESS_LOST_ERRORS = (
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.ChannelPublicGroupNaError,
    errors.UserBannedInChannelError,
    errors.ChatAdminRequiredError,
)


class HistoryScanWorker:
    """Claim persisted scan jobs and resume safely after process interruptions."""

    def __init__(self, repository: ScanRepository, client: TelegramClient, bot: Bot) -> None:
        self._repository = repository
        self._client = client
        self._bot = bot

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                lease = await self._repository.claim_next_job()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scan_job_claim_failed")
                await _wait_for_stop(stop_event, 2)
                continue
            if lease is None:
                await _wait_for_stop(stop_event, 1)
                continue
            await self._run_job(lease)

    async def _run_job(self, lease: ScanJobLease) -> None:
        try:
            await self._scan_pages(lease)
        except asyncio.CancelledError:
            raise
        except errors.FloodWaitError as exc:
            await self._repository.postpone_job(
                lease.id, "Telegram flood wait", retry_after=int(getattr(exc, "seconds", 60))
            )
        except _ACCESS_LOST_ERRORS:
            notification_chat_id = await self._repository.mark_access_lost(lease.id)
            if notification_chat_id is not None:
                await self._send_access_lost(notification_chat_id, lease.group_title)
        except errors.RPCError:
            await self._repository.postpone_job(lease.id, "Telegram RPC error")
        except Exception:
            logger.exception("scan_job_failed")
            await self._repository.postpone_job(lease.id, "Temporary scan error")

    async def _scan_pages(self, lease: ScanJobLease) -> None:
        keywords = [MatchableKeyword(value=value, normalized_value=value) for value in lease.keyword_snapshot]
        cursor = lease.cursor_message_id
        high_watermark = lease.high_watermark_message_id
        while True:
            page = await self._read_page(lease, cursor)
            if not page:
                completion = await self._repository.complete_job(lease.id)
                await self._send_completion(completion)
                return

            reached_lower_boundary = False
            checked = 0
            matches: list[ScanMatch] = []
            page_high_watermark: int | None = None
            for message in page:
                if lease.topic_id is not None and not message_in_topic(message, lease.topic_id):
                    continue
                message_date = message.date
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=UTC)
                if message_date > lease.range_end:
                    continue
                if message_date < lease.range_start:
                    reached_lower_boundary = True
                    continue
                checked += 1
                if page_high_watermark is None or message.id > page_high_watermark:
                    page_high_watermark = message.id
                text = message.raw_text or ""
                matched_keywords = find_matching_keywords(text, keywords)
                if not matched_keywords:
                    continue
                matches.append(
                    ScanMatch(
                        telegram_message_id=message.id,
                        message_date=message_date,
                        matched_keywords=matched_keywords,
                        payload=format_notification(
                            group_title=lease.group_title,
                            group_username=lease.group_username,
                            telegram_chat_id=lease.telegram_chat_id,
                            telegram_message_id=message.id,
                            message_date=message_date,
                            matched_keywords=matched_keywords,
                            text=text,
                        ),
                    )
                )
            if page_high_watermark is not None and (
                high_watermark is None or page_high_watermark > high_watermark
            ):
                high_watermark = page_high_watermark
            next_cursor = min(message.id for message in page)
            progress = await self._repository.persist_page(
                job_id=lease.id,
                cursor_message_id=next_cursor,
                high_watermark_message_id=high_watermark,
                messages_checked=checked,
                matches=matches,
            )
            if not progress.continue_scanning:
                return
            if reached_lower_boundary or len(page) < PAGE_SIZE:
                completion = await self._repository.complete_job(lease.id)
                await self._send_completion(completion)
                return
            cursor = next_cursor

    async def _read_page(self, lease: ScanJobLease, cursor: int | None):
        options = {
            "limit": PAGE_SIZE,
            "offset_id": cursor or 0,
            "min_id": lease.resume_after_message_id or 0,
        }
        if lease.topic_id is not None:
            options["reply_to"] = lease.topic_id
        return [
            message
            async for message in self._client.iter_messages(
                lease.telegram_chat_id,
                **options,
            )
        ]

    async def _send_completion(self, completion: ScanCompletion | None) -> None:
        if completion is None or completion.notification_chat_id is None:
            return
        if completion.job_type == ScanJobType.RECOVERY and completion.matches_found == 0:
            return
        mode = {
            ScanJobType.INITIAL_SEVEN_DAYS: "Сканирование последних 7 дней завершено",
            ScanJobType.MANUAL_SEVEN_DAYS: "Повторное сканирование завершено",
            ScanJobType.RECOVERY: "Восстановление после перезапуска завершено",
        }[completion.job_type]
        try:
            await self._bot.send_message(
                completion.notification_chat_id,
                f"{mode}: «{completion.group_title[:200]}».\n"
                f"Проверено сообщений: {completion.messages_checked}. "
                f"Новых совпадений: {completion.matches_found}.",
            )
        except Exception:
            logger.exception("scan_completion_report_failed")

    async def _send_access_lost(self, notification_chat_id: int, group_title: str) -> None:
        try:
            await self._bot.send_message(
                notification_chat_id,
                f"Нет доступа к группе «{group_title[:200]}». Мониторинг приостановлен. "
                "Проверьте, что подключённый Telegram-аккаунт всё ещё состоит в группе.",
            )
        except Exception:
            logger.exception("access_lost_report_failed")


async def _wait_for_stop(stop_event: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout)
    except TimeoutError:
        pass
