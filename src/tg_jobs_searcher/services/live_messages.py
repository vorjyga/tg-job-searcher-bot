"""Persist live message matches before handing notifications to the outbox worker."""

from __future__ import annotations

import logging
from datetime import datetime

from tg_jobs_searcher.db.repositories import MonitoringRepository
from tg_jobs_searcher.services.matching import find_matching_keywords
from tg_jobs_searcher.services.notifications import format_notification

logger = logging.getLogger(__name__)


class LiveMessageProcessor:
    def __init__(self, repository: MonitoringRepository) -> None:
        self._repository = repository

    async def process(
        self,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
        message_date: datetime,
        text: str,
    ) -> int:
        """Match one new message and enqueue at most one durable notification per group."""
        enqueued_count = 0
        for group in await self._repository.active_groups_for_chat(telegram_chat_id, message_date):
            matched_keywords = find_matching_keywords(text, group.keywords)
            if not matched_keywords:
                continue
            payload = format_notification(
                group_title=group.title,
                group_username=group.username,
                telegram_chat_id=group.telegram_chat_id,
                telegram_message_id=telegram_message_id,
                message_date=message_date,
                matched_keywords=matched_keywords,
                text=text,
            )
            if await self._repository.record_live_match(
                group_id=group.id,
                telegram_message_id=telegram_message_id,
                message_date=message_date,
                matched_keywords=matched_keywords,
                payload=payload,
            ):
                enqueued_count += 1
        if enqueued_count:
            logger.info("live_message_matches_enqueued", extra={"count": enqueued_count})
        return enqueued_count
