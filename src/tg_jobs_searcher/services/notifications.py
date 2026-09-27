"""Notification formatting and durable Bot API delivery worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from tg_jobs_searcher.db.notification_repository import NotificationRepository
from tg_jobs_searcher.db.repository_types import Delivery
from tg_jobs_searcher.services.pause import PauseCoordinator

logger = logging.getLogger(__name__)


def format_notification(
    *,
    group_title: str,
    group_username: str | None,
    telegram_chat_id: int,
    telegram_message_id: int,
    message_date: datetime,
    matched_keywords: list[str],
    text: str,
) -> str:
    """Create a plain-text notification that never exceeds Telegram's text limit."""
    timestamp = message_date.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    keywords_text = _truncate(", ".join(matched_keywords), 1_000)
    prefix = (
        "Найдено совпадение\n"
        f"Источник: {group_title.replace(chr(10), ' ')[:255]}\n"
        f"Совпавшие условия: {keywords_text}\n"
        f"Дата: {timestamp}\n\n"
    )
    link = _message_link(group_username, telegram_chat_id, telegram_message_id)
    suffix = f"\n\nОткрыть сообщение: {link}" if link else ""
    available_text_length = max(0, 4096 - len(prefix) - len(suffix))
    excerpt = text.strip()
    if len(excerpt) > available_text_length:
        excerpt = excerpt[: max(0, available_text_length - 1)].rstrip() + "…"
    return prefix + excerpt + suffix


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 1].rstrip() + "…"


class NotificationWorker:
    """Poll PostgreSQL outbox rows and deliver them through the Bot API."""

    def __init__(
        self, repository: NotificationRepository, bot: Bot, pause: PauseCoordinator | None = None
    ) -> None:
        self._repository = repository
        self._bot = bot
        self._pause = pause

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            if self._pause is not None and self._pause.is_paused:
                await _wait_for_stop(stop_event, 1)
                continue
            try:
                await self._repository.requeue_running()
                deliveries = await self._repository.claim_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("notification_outbox_claim_failed")
                await _wait_for_stop(stop_event, 2)
                continue
            if not deliveries:
                await _wait_for_stop(stop_event, 1)
                continue
            for delivery in deliveries:
                try:
                    await self._deliver(delivery)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("notification_outbox_delivery_state_failed")

    async def _deliver(self, delivery: Delivery) -> None:
        if self._pause is not None:
            async with self._pause.activity() as allowed:
                if not allowed:
                    return
                await self._deliver_active(delivery)
            return
        await self._deliver_active(delivery)

    async def _deliver_active(self, delivery: Delivery) -> None:
        if not await self._repository.is_deliverable(delivery):
            return
        try:
            message = await self._bot.send_message(delivery.notification_chat_id, delivery.payload)
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as exc:
            await self._repository.mark_failed(delivery.id, str(exc), retry_after=int(exc.retry_after))
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            await self._repository.mark_failed(delivery.id, str(exc), permanent=True)
        except Exception as exc:
            await self._repository.mark_failed(delivery.id, str(exc))
        else:
            await self._repository.mark_sent(delivery.id, message.message_id)


def _message_link(username: str | None, chat_id: int, message_id: int) -> str | None:
    if username:
        return f"https://t.me/{username}/{message_id}"
    chat_id_text = str(chat_id)
    if chat_id_text.startswith("-100"):
        return f"https://t.me/c/{chat_id_text.removeprefix('-100')}/{message_id}"
    return None


async def _wait_for_stop(stop_event: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout)
    except TimeoutError:
        pass
