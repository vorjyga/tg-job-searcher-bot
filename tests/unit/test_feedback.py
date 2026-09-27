import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, User

from tg_jobs_searcher.bot.management import _send_feedback, create_management_router
from tg_jobs_searcher.db.repositories import ConversationStep


def _message(text: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Mike", username="mike"),
        text=text,
    )


@pytest.mark.asyncio
async def test_feedback_dialog_forwards_message_to_admin_and_clears_state() -> None:
    owner_id = uuid.uuid4()
    owners = SimpleNamespace(ensure_owner=AsyncMock(return_value=SimpleNamespace(id=owner_id)))
    conversations = SimpleNamespace(
        set=AsyncMock(),
        get=AsyncMock(return_value=SimpleNamespace(step=ConversationStep.AWAITING_FEEDBACK.value)),
        clear=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch("tg_jobs_searcher.bot.management.ConversationRepository", return_value=conversations):
        router = create_management_router(
            admin_telegram_id=1466409,
            owners=owners,
            session_factory=None,  # type: ignore[arg-type]
            resolve_group=AsyncMock(),
            check_group_access=AsyncMock(),
        )

    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await router.message.handlers[2].callback(
            _message("/feedback"), SimpleNamespace(args=None), bot
        )
        await router.message.handlers[-1].callback(_message("Добавьте фильтр по дате"), bot)

    conversations.set.assert_awaited_once_with(owner_id, ConversationStep.AWAITING_FEEDBACK, {})
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 1466409
    assert "Добавьте фильтр по дате" in bot.send_message.await_args.args[1]
    assert "Telegram ID: 42" in bot.send_message.await_args.args[1]
    conversations.clear.assert_awaited_once_with(owner_id)
    assert "Спасибо" in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_feedback_rejects_oversized_message_without_notifying_admin() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        sent = await _send_feedback(_message("idea"), bot, 1466409, "x" * 3001)

    assert not sent
    bot.send_message.assert_not_awaited()
    assert "3000" in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_feedback_failure_asks_user_to_retry() -> None:
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=TelegramForbiddenError(
                method=SendMessage(chat_id=1466409, text="feedback"),
                message="Forbidden: bot was blocked by the user",
            )
        )
    )
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        sent = await _send_feedback(_message("idea"), bot, 1466409, "idea")

    assert not sent
    assert "Попробуйте позже" in answer.await_args.args[0]
