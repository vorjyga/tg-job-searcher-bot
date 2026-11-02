import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from tg_jobs_searcher.bot.management import create_management_router
from tg_jobs_searcher.bot.management_views import _card_keyboard
from tg_jobs_searcher.db.models import GroupStatus
from tg_jobs_searcher.db.repository_types import ConversationStep, GroupCard, KeywordSummary
from tg_jobs_searcher.telegram.client import ResolvedGroup


@pytest.fixture
def menu():
    owner = SimpleNamespace(id=uuid.uuid4())
    owners = SimpleNamespace(
        is_authorized=AsyncMock(return_value=True),
        ensure_owner=AsyncMock(return_value=owner),
    )
    groups = SimpleNamespace(
        list_shared_keywords=AsyncMock(return_value=[]),
        append_shared_keywords=AsyncMock(),
        replace_shared_keywords=AsyncMock(),
        delete_shared_keyword=AsyncMock(return_value=True),
        list_groups=AsyncMock(),
        get_keywords=AsyncMock(return_value=[]),
        find_active_by_telegram_id=AsyncMock(return_value=None),
    )
    conversations = SimpleNamespace(
        set=AsyncMock(), clear=AsyncMock(), get=AsyncMock(return_value=None)
    )
    resolve_group = AsyncMock()
    with (
        patch("tg_jobs_searcher.bot.management.GroupRepository", return_value=groups),
        patch("tg_jobs_searcher.bot.management.ConversationRepository", return_value=conversations),
    ):
        router = create_management_router(
            admin_telegram_id=1,
            owners=owners,
            session_factory=None,
            resolve_group=resolve_group,
            check_group_access=AsyncMock(),
        )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return SimpleNamespace(
        dispatcher=dispatcher,
        groups=groups,
        owners=owners,
        owner=owner,
        conversations=conversations,
        resolve_group=resolve_group,
    )


async def feed(menu, *, text=None, data=None):
    user = User(id=42, first_name="User", is_bot=False)
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=user,
        text=text or "Menu",
    )
    update = (
        Update(update_id=1, message=message)
        if data is None
        else Update(
            update_id=1,
            callback_query=CallbackQuery(
                id="test",
                from_user=user,
                chat_instance="private",
                message=message,
                data=data,
            ),
        )
    )
    bot = Bot("123456:TEST")
    try:
        await menu.dispatcher.feed_update(bot, update)
    finally:
        await bot.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [True, False])
async def test_keyword_menu_opens_without_groups(menu, command):
    with (
        patch.object(Message, "answer", new_callable=AsyncMock) as answer,
        patch.object(Message, "edit_text", new_callable=AsyncMock) as edit,
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
    ):
        await feed(
            menu, text="/keywords" if command else None, data=None if command else "keywords:open"
        )
    output = answer if command else edit
    assert "Ключевые слова для всех ваших групп" in output.await_args.args[0]
    buttons = [b for row in output.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert {b.callback_data for b in buttons} == {"keywords:add", "keywords:replace", "menu:open"}
    menu.groups.list_shared_keywords.assert_awaited_once_with(menu.owner.id)
    menu.groups.list_groups.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,step",
    [
        ("add", ConversationStep.AWAITING_APPEND_KEYWORDS),
        ("replace", ConversationStep.AWAITING_REPLACE_KEYWORDS),
    ],
)
async def test_keyword_buttons_start_persisted_global_dialog(menu, action, step):
    with (
        patch.object(Message, "answer", new_callable=AsyncMock) as answer,
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
    ):
        await feed(menu, data=f"keywords:{action}")
    menu.conversations.set.assert_awaited_once_with(menu.owner.id, step, {})
    assert (
        "во всех ваших группах" in answer.await_args.args[0]
        or "для всех ваших групп" in answer.await_args.args[0]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_keyword_dialog_saves_without_group_context_and_keeps_invalid_input(menu, valid):
    menu.conversations.get.return_value = SimpleNamespace(
        step=ConversationStep.AWAITING_REPLACE_KEYWORDS.value,
        draft={},
    )
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await feed(menu, text="python & remote, backend" if valid else "python &")
    if valid:
        args = menu.groups.replace_shared_keywords.await_args.args
        assert args[0] == menu.owner.id
        assert [k.terms for k in args[1]] == [("python", "remote"), ("backend",)]
        menu.conversations.clear.assert_awaited_once_with(menu.owner.id)
        assert "Условия сохранены для всех" in answer.await_args.args[0]
    else:
        menu.groups.replace_shared_keywords.assert_not_awaited()
        menu.conversations.clear.assert_not_awaited()
        assert "Не удалось сохранить" in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_keyword_removal_uses_owner_and_pagination_without_group(menu):
    keywords = [KeywordSummary(uuid.uuid4(), f"rule{i}") for i in range(45)]
    menu.groups.list_shared_keywords.return_value = keywords
    with (
        patch.object(Message, "edit_text", new_callable=AsyncMock) as edit,
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
    ):
        await feed(menu, data="keywords:page:1")
        buttons = [b for row in edit.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        assert buttons[0].callback_data == f"key:delete:{keywords[20].id.hex}"
        assert buttons[-1].callback_data == "keywords:open"
        await feed(menu, data=f"key:delete:{keywords[20].id.hex}")
    menu.groups.delete_shared_keyword.assert_awaited_once_with(menu.owner.id, keywords[20].id)
    assert "Ключевые слова для всех" in edit.await_args.args[0]


@pytest.mark.asyncio
async def test_unauthorized_user_cannot_open_keyword_menu(menu):
    menu.owners.is_authorized.return_value = False
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await feed(menu, text="/keywords")
    menu.groups.list_shared_keywords.assert_not_awaited()
    answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_back_button_returns_to_main_menu(menu):
    with (
        patch.object(Message, "edit_text", new_callable=AsyncMock) as edit,
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
    ):
        await feed(menu, data="menu:open")
    buttons = [b for row in edit.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(b.callback_data == "keywords:open" for b in buttons)
    menu.conversations.clear.assert_awaited_once_with(menu.owner.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["groups:setup", "groups:add_hint"])
async def test_personal_setup_asks_for_keywords_before_group(menu, data):
    with (
        patch.object(Message, "answer", new_callable=AsyncMock) as answer,
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
    ):
        await feed(menu, data=data)
    menu.conversations.set.assert_awaited_once_with(
        menu.owner.id, ConversationStep.AWAITING_PERSONAL_KEYWORDS, {}
    )
    assert "Сначала введите ключевые слова" in answer.await_args.args[0]
    menu.resolve_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_setup_saves_keywords_then_uses_them_for_submitted_group(menu):
    menu.conversations.get.return_value = SimpleNamespace(
        step=ConversationStep.AWAITING_PERSONAL_KEYWORDS.value, draft={}
    )
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await feed(menu, text="python & remote, backend")
        owner_id, keywords = menu.groups.append_shared_keywords.await_args.args
        assert owner_id == menu.owner.id
        assert [k.terms for k in keywords] == [("python", "remote"), ("backend",)]
        owner_id, step, draft = menu.conversations.set.await_args.args
        assert step == ConversationStep.AWAITING_GROUP
        assert draft["nonce"]
        assert "Теперь добавим первый источник" in answer.await_args.args[0]
        menu.groups.replace_shared_keywords.assert_not_awaited()

        menu.conversations.get.return_value = SimpleNamespace(step=step.value, draft=draft)
        menu.groups.get_keywords.return_value = keywords
        menu.resolve_group.return_value = ResolvedGroup(-100123, "Jobs", "jobs")
        await feed(menu, text="@jobs")

    menu.resolve_group.assert_awaited_once_with("@jobs")
    owner_id, step, draft = menu.conversations.set.await_args.args
    assert owner_id == menu.owner.id
    assert step == ConversationStep.AWAITING_MODE
    assert draft["group"]["telegram_chat_id"] == -100123
    assert draft["use_shared_keywords"] is True
    assert "python & remote" in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_invalid_personal_keywords_keep_dialog_and_do_not_request_group(menu):
    menu.conversations.get.return_value = SimpleNamespace(
        step=ConversationStep.AWAITING_PERSONAL_KEYWORDS.value, draft={}
    )
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await feed(menu, text="python &")
    menu.groups.append_shared_keywords.assert_not_awaited()
    menu.conversations.set.assert_not_awaited()
    menu.conversations.clear.assert_not_awaited()
    assert "Не удалось сохранить" in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_unauthorized_user_cannot_start_personal_setup(menu):
    menu.owners.is_authorized.return_value = False
    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock):
        await feed(menu, data="groups:setup")
    menu.conversations.set.assert_not_awaited()


def test_group_card_has_no_keyword_edit_actions():
    card = GroupCard(
        id=uuid.uuid4(),
        title="Jobs",
        telegram_chat_id=-100123,
        username=None,
        status=GroupStatus.ACTIVE,
        keywords=[],
    )
    buttons = [b.callback_data for row in _card_keyboard(card).inline_keyboard for b in row]
    assert not any("keys" in data for data in buttons)
    assert "menu:open" in buttons
