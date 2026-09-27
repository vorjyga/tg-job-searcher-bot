import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tg_jobs_searcher.db.repositories import DailyAnalytics, due_report_day, today_window_utc3
from tg_jobs_searcher.services.analytics import (
    DailyAnalyticsWorker,
    format_daily_report,
    format_today_report,
)


def test_report_is_due_after_nine_in_tbilisi_and_catches_up_missed_days() -> None:
    # 05:00 UTC is 09:00 in Tbilisi.
    before = datetime(2026, 9, 28, 4, 59, tzinfo=UTC)
    after = datetime(2026, 9, 28, 5, 0, tzinfo=UTC)

    assert due_report_day(date(2026, 9, 26), before) is None
    assert due_report_day(date(2026, 9, 26), after) == date(2026, 9, 27)
    assert due_report_day(date(2026, 9, 24), after) == date(2026, 9, 25)


def test_report_formats_all_requested_counts() -> None:
    report = format_daily_report(
        date(2026, 9, 27),
        DailyAnalytics(users_joined=3, users_blocked=1, groups_added=4, groups_removed=2),
    )

    assert "27.09.2026" in report
    assert "Новых пользователей: 3" in report
    assert "Заблокировали бота: 1" in report
    assert "Групп добавлено: 4" in report
    assert "Групп удалено: 2" in report


def test_today_window_uses_fixed_gmt_plus_three_even_across_utc_date_boundary() -> None:
    day, start, end = today_window_utc3(datetime(2026, 9, 27, 22, 30, tzinfo=UTC))

    assert day == date(2026, 9, 28)
    assert start == datetime(2026, 9, 27, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 27, 22, 30, tzinfo=UTC)


def test_today_report_names_requested_window_and_counts() -> None:
    report = format_today_report(
        date(2026, 9, 28),
        datetime(2026, 9, 28, 5, 7, tzinfo=UTC),
        DailyAnalytics(users_joined=2, users_blocked=1, groups_added=3, groups_removed=4),
    )

    assert "28.09.2026 (GMT+3, 00:00–08:07)" in report
    assert "Новых пользователей: 2" in report
    assert "Заблокировали бота: 1" in report
    assert "Групп добавлено: 3" in report
    assert "Групп удалено: 4" in report


@pytest.mark.asyncio
async def test_worker_marks_day_sent_only_after_admin_message() -> None:
    stop_event = asyncio.Event()
    repository = SimpleNamespace(
        next_due_day=AsyncMock(return_value=date(2026, 9, 27)),
        counts_for_day=AsyncMock(return_value=DailyAnalytics(users_joined=2)),
        mark_report_sent=AsyncMock(side_effect=lambda _: stop_event.set()),
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    await DailyAnalyticsWorker(repository, bot, 1466409).run(stop_event)

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 1466409
    repository.mark_report_sent.assert_awaited_once_with(date(2026, 9, 27))
