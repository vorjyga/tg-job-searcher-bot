from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from tg_jobs_searcher.bot.handlers import INVITE_CALLBACK_DATA, create_public_router, create_router


def _message(user: User, *, chat_type: str = "private") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=user.id, type=chat_type),
        from_user=user,
        text="/start",
    )


def _callback(user: User) -> CallbackQuery:
    return CallbackQuery(
        id="callback-1",
        from_user=user,
        chat_instance="private-chat",
        message=_message(user),
        data=INVITE_CALLBACK_DATA,
    )


def _router(*, authorized: bool = False):
    owners = SimpleNamespace(is_authorized=AsyncMock(return_value=authorized))
    register_owner = AsyncMock()
    router = create_public_router(
        admin_telegram_id=1466409,
        owners=owners,
        register_owner=register_owner,
    )
    return router, owners, register_owner


@pytest.mark.asyncio
async def test_unauthorized_start_offers_invite_without_registering_user() -> None:
    router, _, register_owner = _router()
    user = User(id=42, is_bot=False, first_name="New")

    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await router.message.handlers[0].callback(_message(user))

    register_owner.assert_not_awaited()
    assert "приглашению" in answer.await_args.args[0]
    button = answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Запросить инвайт"
    assert button.callback_data == INVITE_CALLBACK_DATA


@pytest.mark.asyncio
async def test_authorized_start_keeps_existing_greeting() -> None:
    router, _, register_owner = _router(authorized=True)
    user = User(id=1466409, is_bot=False, first_name="Admin")

    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await router.message.handlers[0].callback(_message(user))

    register_owner.assert_awaited_once_with(1466409, 1466409)
    assert "/add_user" in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_invite_request_notifies_admin_once() -> None:
    router, _, _ = _router()
    user = User(id=42, is_bot=False, first_name="New", username="new_user")
    bot = SimpleNamespace(send_message=AsyncMock())
    handler = router.callback_query.handlers[0].callback

    with (
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer,
        patch.object(Message, "edit_text", new_callable=AsyncMock) as edit_text,
    ):
        await handler(_callback(user), bot)
        await handler(_callback(user), bot)

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 1466409
    assert "@new_user" in bot.send_message.await_args.args[1]
    assert "/add_user 42" in bot.send_message.await_args.args[1]
    assert "Запрос отправлен" in edit_text.await_args.args[0]
    assert answer.await_count == 2


@pytest.mark.asyncio
async def test_invite_button_does_not_notify_admin_after_access_granted() -> None:
    router, _, _ = _router(authorized=True)
    user = User(id=42, is_bot=False, first_name="New")
    bot = SimpleNamespace(send_message=AsyncMock())

    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer:
        await router.callback_query.handlers[0].callback(_callback(user), bot)

    bot.send_message.assert_not_awaited()
    assert "уже есть доступ" in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_public_start_works_while_other_commands_remain_protected() -> None:
    router, owners, _ = _router()
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher.include_router(
        create_router(
            admin_telegram_id=1466409,
            owners=owners,
            resolve_group=AsyncMock(),
            list_groups=AsyncMock(),
        )
    )
    bot = Bot(token="123456:TEST")
    user = User(id=42, is_bot=False, first_name="New")

    try:
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            await dispatcher.feed_update(bot, Update(update_id=1, message=_message(user)))
            await dispatcher.feed_update(
                bot,
                Update(update_id=2, message=_message(user).model_copy(update={"text": "/help"})),
            )
        answer.assert_awaited_once()
        assert "приглашению" in answer.await_args.args[0]
    finally:
        await bot.session.close()
