import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage

from tg_jobs_searcher.db.repositories import Delivery
from tg_jobs_searcher.services.notifications import NotificationWorker, format_notification


def test_formats_notification_with_internal_group_link_and_safe_length() -> None:
    payload = format_notification(
        group_title="Backend jobs",
        group_username=None,
        telegram_chat_id=-1001234567890,
        telegram_message_id=77,
        message_date=datetime(2026, 9, 15, 10, 5, tzinfo=UTC),
        matched_keywords=["keyword" * 100 for _ in range(50)],
        text="x" * 5000,
    )

    assert len(payload) <= 4096
    assert "https://t.me/c/1234567890/77" in payload
    assert "2026-09-15 10:05 UTC" in payload


class FakeRepository:
    def __init__(self) -> None:
        self.sent: list[tuple[uuid.UUID, int]] = []
        self.failed: list[tuple[uuid.UUID, str]] = []
        self.failed_options: list[dict] = []
        self.deliverable = True

    async def is_deliverable(self, delivery: Delivery) -> bool:
        return self.deliverable

    async def mark_sent(self, outbox_id: uuid.UUID, message_id: int) -> None:
        self.sent.append((outbox_id, message_id))

    async def mark_failed(self, outbox_id: uuid.UUID, error: str, **kwargs) -> None:
        self.failed.append((outbox_id, error))
        self.failed_options.append(kwargs)


class FakeBot:
    async def send_message(self, chat_id: int, payload: str):
        assert chat_id == 42
        assert payload == "payload"
        return SimpleNamespace(message_id=99)


@pytest.mark.asyncio
async def test_worker_marks_sent_after_bot_api_success() -> None:
    repository = FakeRepository()
    worker = NotificationWorker(repository, FakeBot())  # type: ignore[arg-type]
    delivery = Delivery(id=uuid.uuid4(), notification_chat_id=42, payload="payload")

    await worker._deliver(delivery)

    assert repository.sent == [(delivery.id, 99)]
    assert repository.failed == []


@pytest.mark.asyncio
async def test_worker_skips_delivery_revoked_after_batch_claim() -> None:
    repository = FakeRepository()
    repository.deliverable = False
    worker = NotificationWorker(repository, FakeBot())  # type: ignore[arg-type]
    delivery = Delivery(id=uuid.uuid4(), notification_chat_id=42, payload="payload")

    await worker._deliver(delivery)

    assert repository.sent == []
    assert repository.failed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramForbiddenError])
async def test_worker_does_not_retry_permanent_bot_api_errors(error_type) -> None:
    repository = FakeRepository()

    class FailingBot:
        async def send_message(self, chat_id: int, payload: str):
            raise error_type(
                method=SendMessage(chat_id=chat_id, text=payload), message="cannot deliver"
            )

    delivery = Delivery(id=uuid.uuid4(), notification_chat_id=42, payload="payload")
    await NotificationWorker(repository, FailingBot())._deliver(delivery)  # type: ignore[arg-type]

    assert repository.failed_options == [{"permanent": True}]
