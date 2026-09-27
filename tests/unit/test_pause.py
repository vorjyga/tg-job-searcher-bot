import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from tg_jobs_searcher.bot.pause import PauseMiddleware
from tg_jobs_searcher.db.repository_types import Delivery
from tg_jobs_searcher.services.notifications import NotificationWorker
from tg_jobs_searcher.services.pause import PauseCoordinator


class FakeControlRepository:
    def __init__(self, paused: bool = False) -> None:
        self.paused = paused

    async def is_paused(self) -> bool:
        return self.paused

    async def set_paused(self, paused: bool) -> bool:
        changed = self.paused != paused
        self.paused = paused
        return changed


def _message(user_id: int, text: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="User"),
        text=text,
    )


@pytest.mark.asyncio
async def test_pause_waits_for_current_action_and_survives_new_coordinator() -> None:
    repository = FakeControlRepository()
    coordinator = PauseCoordinator(repository)  # type: ignore[arg-type]
    await coordinator.initialize()
    action_started = asyncio.Event()
    release_action = asyncio.Event()

    async def current_action() -> None:
        async with coordinator.activity() as allowed:
            assert allowed
            action_started.set()
            await release_action.wait()

    action = asyncio.create_task(current_action())
    await action_started.wait()
    pausing = asyncio.create_task(coordinator.pause())
    await asyncio.sleep(0)
    assert coordinator.is_paused
    assert not pausing.done()
    async with coordinator.activity() as allowed:
        assert not allowed

    release_action.set()
    await action
    assert await pausing
    restarted = PauseCoordinator(repository)  # type: ignore[arg-type]
    await restarted.initialize()
    assert restarted.is_paused
    assert await restarted.resume()
    async with restarted.activity() as allowed:
        assert allowed


@pytest.mark.asyncio
async def test_paused_middleware_blocks_users_but_allows_admin_resume() -> None:
    coordinator = PauseCoordinator(FakeControlRepository(True))  # type: ignore[arg-type]
    await coordinator.initialize()
    middleware = PauseMiddleware(coordinator, admin_telegram_id=1466409)
    handler = AsyncMock(return_value="handled")

    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        assert await middleware(handler, _message(42, "/add"), {}) is None
        assert "на паузе" in answer.await_args.args[0]
    handler.assert_not_awaited()

    assert await middleware(handler, _message(1466409, "/resume"), {}) == "handled"

    callback = CallbackQuery(
        id="paused",
        from_user=User(id=42, is_bot=False, first_name="User"),
        chat_instance="private-chat",
        message=_message(42, "Group"),
        data="group:open:any",
    )
    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer:
        assert await middleware(handler, callback, {}) is None
        assert answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_pause_waits_for_delivery_and_blocks_the_next_one() -> None:
    coordinator = PauseCoordinator(FakeControlRepository())  # type: ignore[arg-type]
    await coordinator.initialize()
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def send_message(_chat_id, _payload):
        send_started.set()
        await release_send.wait()
        return SimpleNamespace(message_id=99)

    repository = SimpleNamespace(
        is_deliverable=AsyncMock(return_value=True),
        mark_sent=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send_message))
    worker = NotificationWorker(repository, bot, coordinator)  # type: ignore[arg-type]
    first = Delivery(id=uuid.uuid4(), notification_chat_id=42, payload="first")
    second = Delivery(id=uuid.uuid4(), notification_chat_id=42, payload="second")

    sending = asyncio.create_task(worker._deliver(first))
    await send_started.wait()
    pausing = asyncio.create_task(coordinator.pause())
    await asyncio.sleep(0)
    assert not pausing.done()
    release_send.set()
    await sending
    await pausing

    await worker._deliver(second)
    assert bot.send_message.await_count == 1
    assert repository.mark_sent.await_count == 1

    await coordinator.resume()
    await worker._deliver(second)
    assert bot.send_message.await_count == 2
