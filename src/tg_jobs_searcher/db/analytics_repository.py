"""Daily analytics persistence and report scheduling."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.db.models import (
    AnalyticsEvent,
    AnalyticsEventType,
    AnalyticsReportState,
)
from tg_jobs_searcher.db.repository_types import (
    REPORT_TIMEZONE,
    DailyAnalytics,
    due_report_day,
)


class AnalyticsRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def next_due_day(self, now: datetime) -> date | None:
        local_now = now.astimezone(REPORT_TIMEZONE)
        baseline = local_now.date() - timedelta(days=1)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                insert(AnalyticsReportState)
                .values(id=1, last_reported_date=baseline)
                .on_conflict_do_nothing(index_elements=[AnalyticsReportState.id])
            )
            last_reported = await session.scalar(
                select(AnalyticsReportState.last_reported_date).where(AnalyticsReportState.id == 1)
            )
        assert last_reported is not None
        return due_report_day(last_reported, now)

    async def counts_for_day(self, day: date) -> DailyAnalytics:
        start = datetime.combine(day, time.min, REPORT_TIMEZONE).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time.min, REPORT_TIMEZONE).astimezone(UTC)
        return await self.counts_between(start, end)

    async def counts_between(self, start: datetime, end: datetime) -> DailyAnalytics:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    AnalyticsEvent.event_type,
                    func.count(AnalyticsEvent.id),
                    func.count(func.distinct(AnalyticsEvent.telegram_user_id)),
                )
                .where(AnalyticsEvent.occurred_at >= start, AnalyticsEvent.occurred_at < end)
                .group_by(AnalyticsEvent.event_type)
            )
            counts = {
                event_type: (total, distinct_users) for event_type, total, distinct_users in rows
            }
        return DailyAnalytics(
            users_joined=counts.get(AnalyticsEventType.USER_JOINED.value, (0, 0))[1],
            users_blocked=counts.get(AnalyticsEventType.BOT_BLOCKED.value, (0, 0))[1],
            groups_added=counts.get(AnalyticsEventType.GROUP_ADDED.value, (0, 0))[0],
            groups_removed=counts.get(AnalyticsEventType.GROUP_REMOVED.value, (0, 0))[0],
        )

    async def mark_report_sent(self, day: date) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(AnalyticsReportState)
                .where(
                    AnalyticsReportState.id == 1,
                    AnalyticsReportState.last_reported_date == day - timedelta(days=1),
                )
                .values(last_reported_date=day)
            )
