from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from tg_jobs_searcher.bot.handlers import INVITE_CALLBACK_DATA, create_public_router, create_router


def _message(user: User, text: str = "/start") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=user.id, type="private"),
        from_user=user,
        text=text,
    )


def _router(*, authorized: bool = True, disabled: bool = False, first_start: bool = True):
    owners = SimpleNamespace(
        is_authorized=AsyncMock(return_value=authorized),
        is_disabled=AsyncMock(return_value=disabled),
        start_user=AsyncMock(return_value=(SimpleNamespace(), first_start)),
        record_bot_block_state=AsyncMock(),
    )
    return create_public_router(admin_telegram_id=1466409, owners=owners), owners


@pytest.mark.asyncio
async def test_first_start_opens_access_and_notifies_admin_once() -> None:
    router, owners = _router()
    user = User(id=42, is_bot=False, first_name="Mike", username="mike")
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await router.message.handlers[0].callback(_message(user), bot)

    owners.start_user.assert_awaited_once_with(42, 42)
    assert bot.send_message.await_args.args[0] == 1466409
    assert "Telegram ID: 42" in bot.send_message.await_args.args[1]
    assert "/add — добавить" in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_repeated_start_does_not_notify_admin_again() -> None:
    router, _ = _router(first_start=False)
    user = User(id=42, is_bot=False, first_name="Mike")
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch.object(Message, "answer", new_callable=AsyncMock):
        await router.message.handlers[0].callback(_message(user), bot)
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_manually_disabled_user_remains_blocked() -> None:
    router, owners = _router(authorized=False, disabled=True)
    user = User(id=42, is_bot=False, first_name="Mike")
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await router.message.handlers[0].callback(_message(user), bot)
    owners.start_user.assert_not_awaited()
    assert "закрыт администратором" in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_invite_mode_shows_request_button_to_new_user() -> None:
    router, owners = _router(authorized=False)
    user = User(id=42, is_bot=False, first_name="Mike")
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await router.message.handlers[0].callback(_message(user), bot)
    owners.start_user.assert_not_awaited()
    assert "по приглашению" in answer.await_args.args[0]
    button = answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == INVITE_CALLBACK_DATA


@pytest.mark.asyncio
async def test_invite_request_sends_admin_an_approval_button_once() -> None:
    router, _ = _router(authorized=False)
    user = User(id=42, is_bot=False, first_name="Mike", username="mike")
    callback = CallbackQuery(
        id="invite",
        from_user=user,
        chat_instance="private-chat",
        message=_message(user),
        data=INVITE_CALLBACK_DATA,
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer,
        patch.object(Message, "edit_text", new_callable=AsyncMock),
    ):
        await router.callback_query.handlers[0].callback(callback, bot)
        await router.callback_query.handlers[0].callback(callback, bot)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 1466409
    assert "Telegram ID: 42" in bot.send_message.await_args.args[1]
    button = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "access:approve:42"
    assert "Запрос уже отправлен" in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_disabled_user_cannot_request_invite() -> None:
    router, _ = _router(authorized=False, disabled=True)
    user = User(id=42, is_bot=False, first_name="Mike")
    callback = CallbackQuery(
        id="denied-invite",
        from_user=user,
        chat_instance="private-chat",
        message=_message(user),
        data=INVITE_CALLBACK_DATA,
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer:
        await router.callback_query.handlers[0].callback(callback, bot)
    assert "закрыт администратором" in answer.await_args.args[0]
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_invite_button_directs_user_to_start() -> None:
    router, _ = _router()
    user = User(id=42, is_bot=False, first_name="Mike")
    callback = CallbackQuery(
        id="old-invite",
        from_user=user,
        chat_instance="private-chat",
        message=_message(user),
        data=INVITE_CALLBACK_DATA,
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer:
        await router.callback_query.handlers[0].callback(callback, bot)
    assert "/start" in answer.await_args.args[0]
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_block_and_unblock_updates_are_recorded() -> None:
    router, owners = _router()
    handler = router.my_chat_member.handlers[0].callback
    event = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        old_chat_member=SimpleNamespace(status="member"),
        new_chat_member=SimpleNamespace(status="kicked"),
        date=datetime.now(UTC),
    )
    await handler(event)
    owners.record_bot_block_state.assert_awaited_once_with(
        42, blocked=True, occurred_at=event.date
    )
    owners.record_bot_block_state.reset_mock()
    event.old_chat_member.status = "kicked"
    event.new_chat_member.status = "member"
    await handler(event)
    owners.record_bot_block_state.assert_awaited_once_with(
        42, blocked=False, occurred_at=event.date
    )


@pytest.mark.asyncio
async def test_public_user_can_use_other_commands() -> None:
    router, owners = _router()
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
    assert "my_chat_member" in dispatcher.resolve_used_update_types()
    bot = Bot(token="123456:TEST")
    user = User(id=42, is_bot=False, first_name="Mike")
    try:
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            await dispatcher.feed_update(bot, Update(update_id=1, message=_message(user, "/help")))
        assert "Ботом можно пользоваться" in answer.await_args.args[0]
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_admin_help_lists_admin_commands() -> None:
    router, owners = _router()
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
    admin = User(id=1466409, is_bot=False, first_name="Admin")
    try:
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            await dispatcher.feed_update(bot, Update(update_id=1, message=_message(admin, "/help")))
        help_text = answer.await_args.args[0]
        assert "/pause —" in help_text
        assert "/available_groups —" in help_text
        assert "/status —" in help_text
    finally:
        await bot.session.close()
