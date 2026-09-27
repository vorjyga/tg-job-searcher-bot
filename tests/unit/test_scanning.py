import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tg_jobs_searcher.db.models import ScanJobType
from tg_jobs_searcher.db.repositories import ScanCompletion, ScanJobLease, ScanProgress
from tg_jobs_searcher.services.scanning import HistoryScanWorker, PeriodicRecoveryScheduler


class FakeScanRepository:
    def __init__(self) -> None:
        self.pages: list[dict] = []
        self.completed: list[uuid.UUID] = []

    async def persist_page(self, **kwargs) -> ScanProgress:
        self.pages.append(kwargs)
        return ScanProgress(continue_scanning=True, matches_inserted=len(kwargs["matches"]))

    async def complete_job(self, job_id: uuid.UUID) -> ScanCompletion:
        self.completed.append(job_id)
        return ScanCompletion(
            group_title="Python jobs",
            notification_chat_id=42,
            job_type=ScanJobType.INITIAL_SEVEN_DAYS,
            messages_checked=2,
            matches_found=1,
        )


class FakeClient:
    def __init__(self, messages: list[object]) -> None:
        self.messages = messages
        self.calls: list[dict] = []

    async def iter_messages(self, chat_id: int, **kwargs):
        self.calls.append({"chat_id": chat_id, **kwargs})
        for message in self.messages:
            yield message


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_history_scan_persists_page_before_completing_and_reports_result() -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    repository = FakeScanRepository()
    client = FakeClient(
        [
            SimpleNamespace(id=25, date=now - timedelta(hours=1), raw_text="Python role"),
            SimpleNamespace(id=24, date=now - timedelta(hours=2), raw_text="Other role"),
        ]
    )
    bot = FakeBot()
    lease = ScanJobLease(
        id=uuid.uuid4(),
        group_id=uuid.uuid4(),
        telegram_chat_id=-100123,
        group_title="Python jobs",
        group_username="python_jobs",
        notification_chat_id=42,
        job_type=ScanJobType.INITIAL_SEVEN_DAYS,
        range_start=now - timedelta(days=7),
        range_end=now,
        keyword_snapshot=[{"value": "Python & role", "terms": ["python", "role"]}],
        cursor_message_id=None,
        high_watermark_message_id=None,
        resume_after_message_id=None,
    )

    await HistoryScanWorker(repository, client, bot)._scan_pages(lease)  # type: ignore[arg-type]

    assert repository.pages[0]["cursor_message_id"] == 24
    assert repository.pages[0]["high_watermark_message_id"] == 25
    assert repository.pages[0]["messages_checked"] == 2
    assert len(repository.pages[0]["matches"]) == 1
    assert repository.pages[0]["matches"][0].matched_keywords == ["Python & role"]
    assert repository.completed == [lease.id]
    assert "Новых совпадений: 1" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_recovery_scan_uses_saved_contiguous_message_id() -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    repository = FakeScanRepository()
    client = FakeClient([])
    worker = HistoryScanWorker(repository, client, FakeBot())  # type: ignore[arg-type]
    lease = ScanJobLease(
        id=uuid.uuid4(),
        group_id=uuid.uuid4(),
        telegram_chat_id=-100123,
        group_title="Python jobs",
        group_username=None,
        notification_chat_id=None,
        job_type=ScanJobType.RECOVERY,
        range_start=now - timedelta(days=7),
        range_end=now,
        keyword_snapshot=[],
        cursor_message_id=None,
        high_watermark_message_id=500,
        resume_after_message_id=500,
    )

    await worker._scan_pages(lease)

    assert client.calls[0]["min_id"] == 500
    assert client.calls[0]["offset_id"] == 0


@pytest.mark.asyncio
async def test_topic_scan_requests_only_topic_and_filters_unrelated_messages() -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    repository = FakeScanRepository()
    client = FakeClient([
        SimpleNamespace(id=50, date=now, raw_text="Python", reply_to=SimpleNamespace(reply_to_top_id=46685)),
        SimpleNamespace(id=49, date=now, raw_text="Python", reply_to=SimpleNamespace(reply_to_top_id=999)),
    ])
    lease = ScanJobLease(
        id=uuid.uuid4(), group_id=uuid.uuid4(), telegram_chat_id=-100123,
        group_title="Python jobs", group_username=None, notification_chat_id=None,
        job_type=ScanJobType.MANUAL_SEVEN_DAYS, range_start=now - timedelta(days=7),
        range_end=now, keyword_snapshot=["python"], cursor_message_id=None,
        high_watermark_message_id=None, resume_after_message_id=None, topic_id=46685,
    )

    await HistoryScanWorker(repository, client, FakeBot())._scan_pages(lease)  # type: ignore[arg-type]

    assert client.calls[0]["reply_to"] == 46685
    assert repository.pages[0]["messages_checked"] == 1
    assert len(repository.pages[0]["matches"]) == 1


@pytest.mark.asyncio
async def test_periodic_recovery_schedules_another_scan_without_restart() -> None:
    stop_event = asyncio.Event()

    class Repository:
        calls = 0

        async def schedule_recovery_jobs(self) -> int:
            self.calls += 1
            stop_event.set()
            return 1

    repository = Repository()
    await PeriodicRecoveryScheduler(repository, interval=0.001).run(stop_event)  # type: ignore[arg-type]

    assert repository.calls == 1
