import asyncio
from types import SimpleNamespace

import pytest

from tg_jobs_searcher.bot.app import BotApplication


class WaitingDispatcher:
    def resolve_used_update_types(self):
        return []

    async def start_polling(self, *_args, **_kwargs):
        await asyncio.Event().wait()

    async def stop_polling(self):
        return None


class WaitingWorker:
    async def run(self, _stop_event):
        await asyncio.Event().wait()


class FailingWorker:
    async def run(self, _stop_event):
        raise ValueError("worker failed")


@pytest.mark.asyncio
async def test_notification_worker_failure_stops_application() -> None:
    application = BotApplication(
        bot=SimpleNamespace(),
        dispatcher=WaitingDispatcher(),  # type: ignore[arg-type]
        notification_worker=FailingWorker(),  # type: ignore[arg-type]
        analytics_worker=WaitingWorker(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="worker failed"):
        await application.run_until_stopped(asyncio.Event())


@pytest.mark.asyncio
async def test_external_scan_task_failure_stops_application() -> None:
    application = BotApplication(
        bot=SimpleNamespace(),
        dispatcher=WaitingDispatcher(),  # type: ignore[arg-type]
        notification_worker=WaitingWorker(),  # type: ignore[arg-type]
        analytics_worker=WaitingWorker(),  # type: ignore[arg-type]
    )

    async def fail_scan():
        raise ValueError("scan failed")

    scan_task = asyncio.create_task(fail_scan())
    with pytest.raises(ValueError, match="scan failed"):
        await application.run_until_stopped(asyncio.Event(), (scan_task,))
