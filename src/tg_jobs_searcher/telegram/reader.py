"""Telethon NewMessage subscription for tracked groups."""

from __future__ import annotations

import logging
from datetime import UTC

from telethon import TelegramClient, events

from tg_jobs_searcher.services.live_messages import LiveMessageProcessor
from tg_jobs_searcher.telegram.topics import message_topic_id

logger = logging.getLogger(__name__)


class LiveMessageMonitor:
    def __init__(self, client: TelegramClient, processor: LiveMessageProcessor) -> None:
        self._client = client
        self._processor = processor
        self._registered = False

    async def start(self) -> None:
        if self._registered:
            return
        self._client.add_event_handler(self._handle_new_message, events.NewMessage(incoming=True))
        self._registered = True

    async def stop(self) -> None:
        if not self._registered:
            return
        self._client.remove_event_handler(self._handle_new_message)
        self._registered = False

    async def _handle_new_message(self, event: events.NewMessage.Event) -> None:
        message = event.message
        if event.chat_id is None or message is None or not event.raw_text:
            return
        message_date = message.date
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=UTC)
        try:
            await self._processor.process(
                telegram_chat_id=event.chat_id,
                telegram_message_id=message.id,
                topic_id=message_topic_id(message),
                message_date=message_date,
                text=event.raw_text,
            )
        except Exception:
            # Do not log message content, group title or account credentials.
            logger.exception("live_message_processing_failed")
