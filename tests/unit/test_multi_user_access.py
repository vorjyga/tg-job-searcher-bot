from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from tg_jobs_searcher.bot.handlers import AuthorizedUserMiddleware
from tg_jobs_searcher.bot.users import AdminOnlyMiddleware, create_admin_router, parse_user_id
from tg_jobs_searcher.db.repositories import OwnerRepository


def _event(user_id: int, chat_type: str = "private") -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(type=chat_type),
    )


@pytest.mark.asyncio
async def test_added_user_can_use_private_chat_but_unapproved_user_cannot() -> None:
    owners = SimpleNamespace(is_authorized=AsyncMock(side_effect=lambda user_id: user_id == 42))
    middleware = AuthorizedUserMiddleware(owners)
    handler = AsyncMock(return_value="handled")

    assert await middleware(handler, _event(42), {}) == "handled"
    assert await middleware(handler, _event(99), {}) is None
    assert await middleware(handler, _event(42, "group"), {}) is None
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_admin_can_run_user_management_in_private_chat() -> None:
    middleware = AdminOnlyMiddleware(1466409)
    handler = AsyncMock(return_value="handled")

    assert await middleware(handler, _event(1466409), {}) == "handled"
    assert await middleware(handler, _event(42), {}) is None
    assert await middleware(handler, _event(1466409, "group"), {}) is None
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
    )
    assert await middleware(handler, callback, {}) is None
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_access_bootstraps_without_database_row() -> None:
    repository = OwnerRepository(None, 1466409)  # type: ignore[arg-type]

    assert await repository.is_authorized(1466409)


@pytest.mark.parametrize("value", [None, "", "@user", "-5", "1 2", "0", str(2**63)])
def test_rejects_invalid_telegram_user_ids(value: str | None) -> None:
    assert parse_user_id(value) is None


def test_accepts_numeric_telegram_user_id() -> None:
    assert parse_user_id(" 1466409 ") == 1466409


@pytest.mark.asyncio
async def test_adding_user_notifies_them_after_access_is_granted() -> None:
    owners = SimpleNamespace(grant_user=AsyncMock(return_value=True))
    router = create_admin_router(1466409, owners)
    message = SimpleNamespace(answer=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())

    await router.message.handlers[0].callback(message, SimpleNamespace(args="42"), bot)

    owners.grant_user.assert_awaited_once_with(42)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 42
    assert "открыт доступ" in bot.send_message.await_args.args[1]
    assert "получил уведомление" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_access_remains_granted_when_user_cannot_receive_notification() -> None:
    owners = SimpleNamespace(grant_user=AsyncMock(return_value=True))
    router = create_admin_router(1466409, owners)
    message = SimpleNamespace(answer=AsyncMock())
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=TelegramForbiddenError(
                method=SendMessage(chat_id=42, text="notification"),
                message="Forbidden: bot was blocked by the user",
            )
        )
    )

    await router.message.handlers[0].callback(message, SimpleNamespace(args="42"), bot)

    owners.grant_user.assert_awaited_once_with(42)
    assert "не доставлено" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_existing_user_does_not_receive_duplicate_access_notification() -> None:
    owners = SimpleNamespace(grant_user=AsyncMock(return_value=False))
    router = create_admin_router(1466409, owners)
    message = SimpleNamespace(answer=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())

    await router.message.handlers[0].callback(message, SimpleNamespace(args="42"), bot)

    bot.send_message.assert_not_awaited()
    assert "уже добавлен" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_approval_button_grants_access_and_removes_button() -> None:
    owners = SimpleNamespace(grant_user=AsyncMock(return_value=True))
    router = create_admin_router(1466409, owners)
    bot = SimpleNamespace(send_message=AsyncMock())
    admin = User(id=1466409, is_bot=False, first_name="Admin")
    request = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1466409, type="private"),
        text="Запрос доступа к боту",
    )
    callback = CallbackQuery(
        id="approval",
        from_user=admin,
        chat_instance="admin-chat",
        message=request,
        data="access:approve:42",
    )

    with (
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer,
        patch.object(Message, "edit_text", new_callable=AsyncMock) as edit_text,
    ):
        await router.callback_query.handlers[0].callback(callback, bot)

    owners.grant_user.assert_awaited_once_with(42)
    bot.send_message.assert_awaited_once()
    assert edit_text.await_args.kwargs["reply_markup"] is None
    assert "получил уведомление" in edit_text.await_args.args[0]
    answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_list_includes_admin_active_and_disabled_accounts() -> None:
    owners = SimpleNamespace(list_users=AsyncMock(return_value=[(42, True), (99, False)]))
    router = create_admin_router(1466409, owners)
    message = SimpleNamespace(answer=AsyncMock())

    await router.message.handlers[2].callback(message)

    rendered = message.answer.await_args.args[0]
    assert "1466409 — администратор" in rendered
    assert "Активные пользователи (2)" in rendered
    assert "• 42" in rendered
    assert "Отключённые (1)" in rendered
    assert "• 99" in rendered


@pytest.mark.asyncio
async def test_user_list_sends_all_ids_across_multiple_messages() -> None:
    users = [(user_id, True) for user_id in range(1, 800)]
    owners = SimpleNamespace(list_users=AsyncMock(return_value=users))
    router = create_admin_router(1466409, owners)
    message = SimpleNamespace(answer=AsyncMock())

    await router.message.handlers[2].callback(message)

    chunks = [call.args[0] for call in message.answer.await_args_list]
    assert len(chunks) > 1
    assert all(len(chunk) <= 3800 for chunk in chunks)
    assert "• 799" in "\n".join(chunks)
