"""Daily usage reports sent to the administrator."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime

from aiogram import Bot

from tg_jobs_searcher.db.analytics_repository import AnalyticsRepository
from tg_jobs_searcher.db.repository_types import ON_DEMAND_REPORT_TIMEZONE, DailyAnalytics

logger = logging.getLogger(__name__)


def format_daily_report(day: date, counts: DailyAnalytics) -> str:
    return (
        f"Отчёт за {day:%d.%m.%Y} (Тбилиси):\n"
        f"Новых пользователей: {counts.users_joined}\n"
        f"Заблокировали бота: {counts.users_blocked}\n"
        f"Групп добавлено: {counts.groups_added}\n"
        f"Групп удалено: {counts.groups_removed}"
    )


def format_today_report(day: date, through: datetime, counts: DailyAnalytics) -> str:
    local_time = through.astimezone(ON_DEMAND_REPORT_TIMEZONE)
    return (
        f"Отчёт за сегодня, {day:%d.%m.%Y} (GMT+3, 00:00–{local_time:%H:%M}):\n"
        f"Новых пользователей: {counts.users_joined}\n"
        f"Заблокировали бота: {counts.users_blocked}\n"
        f"Групп добавлено: {counts.groups_added}\n"
        f"Групп удалено: {counts.groups_removed}"
    )


class DailyAnalyticsWorker:
    def __init__(self, repository: AnalyticsRepository, bot: Bot, admin_telegram_id: int) -> None:
        self._repository = repository
        self._bot = bot
        self._admin_telegram_id = admin_telegram_id

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                day = await self._repository.next_due_day(datetime.now(UTC))
                if day is not None:
                    counts = await self._repository.counts_for_day(day)
                    await self._bot.send_message(
                        self._admin_telegram_id, format_daily_report(day, counts)
                    )
                    await self._repository.mark_report_sent(day)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("daily_analytics_report_failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except TimeoutError:
                pass
