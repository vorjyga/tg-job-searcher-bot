import uuid
from datetime import UTC, datetime

import pytest

from tg_jobs_searcher.db.repositories import MonitoredGroup
from tg_jobs_searcher.services.live_messages import LiveMessageProcessor
from tg_jobs_searcher.services.matching import MatchableKeyword


class FakeMonitoringRepository:
    def __init__(self) -> None:
        self.recorded: list[dict] = []
        self.group = MonitoredGroup(
            id=uuid.uuid4(),
            telegram_chat_id=-100123,
            title="Python jobs",
            username="python_jobs",
            keywords=[MatchableKeyword("Python", "python")],
        )

    async def active_groups_for_chat(self, chat_id: int, message_date: datetime):
        assert chat_id == self.group.telegram_chat_id
        return [self.group]

    async def record_live_match(self, **kwargs):
        self.recorded.append(kwargs)
        return True


@pytest.mark.asyncio
async def test_live_message_creates_one_durable_match_with_current_keywords() -> None:
    repository = FakeMonitoringRepository()
    processor = LiveMessageProcessor(repository)  # type: ignore[arg-type]

    count = await processor.process(
        telegram_chat_id=-100123,
        telegram_message_id=44,
        message_date=datetime(2026, 9, 15, 10, tzinfo=UTC),
        text="Ищем Python developer",
    )

    assert count == 1
    assert repository.recorded[0]["matched_keywords"] == ["Python"]
    assert "https://t.me/python_jobs/44" in repository.recorded[0]["payload"]


@pytest.mark.asyncio
async def test_live_message_ignores_other_topics() -> None:
    repository = FakeMonitoringRepository()
    repository.group = MonitoredGroup(
        id=repository.group.id,
        telegram_chat_id=-100123,
        title="Python jobs",
        username="python_jobs",
        keywords=[MatchableKeyword("Python", "python")],
        topic_id=46685,
    )
    processor = LiveMessageProcessor(repository)  # type: ignore[arg-type]
    now = datetime(2026, 9, 15, 10, tzinfo=UTC)

    assert await processor.process(
        telegram_chat_id=-100123, telegram_message_id=50, topic_id=100,
        message_date=now, text="Python role",
    ) == 0
    assert await processor.process(
        telegram_chat_id=-100123, telegram_message_id=51, topic_id=46685,
        message_date=now, text="Python role",
    ) == 1
    assert len(repository.recorded) == 1
