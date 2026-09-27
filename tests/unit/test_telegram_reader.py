from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tg_jobs_searcher.telegram.reader import LiveMessageMonitor


class FakeClient:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def add_event_handler(self, handler, event_builder) -> None:
        self.handlers.append(handler)

    def remove_event_handler(self, handler) -> None:
        self.handlers.remove(handler)


class FakeProcessor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def process(self, **kwargs) -> int:
        self.calls.append(kwargs)
        return 1


@pytest.mark.asyncio
async def test_monitor_registers_once_and_processes_caption_text() -> None:
    client = FakeClient()
    processor = FakeProcessor()
    monitor = LiveMessageMonitor(client, processor)  # type: ignore[arg-type]

    await monitor.start()
    await monitor.start()
    await monitor._handle_new_message(
        SimpleNamespace(
            chat_id=-100123,
            raw_text="Python vacancy",
            message=SimpleNamespace(id=12, date=datetime(2026, 9, 15, 10)),
        )
    )
    await monitor.stop()

    assert client.handlers == []
    assert processor.calls == [
        {
            "telegram_chat_id": -100123,
            "telegram_message_id": 12,
            "topic_id": 12,
            "message_date": datetime(2026, 9, 15, 10, tzinfo=UTC),
            "text": "Python vacancy",
        }
    ]
