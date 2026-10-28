import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from tg_jobs_searcher.bot.management import create_management_router
from tg_jobs_searcher.bot.management_dialogs import (
    _receive_group_keywords,
    _receive_group_reference,
    _receive_initial_keywords,
)
from tg_jobs_searcher.bot.management_views import _keyword_selection_keyboard
from tg_jobs_searcher.db.models import GroupStatus
from tg_jobs_searcher.db.repository_types import ConversationStep, GroupCard, KeywordSummary
from tg_jobs_searcher.services.keywords import KeywordInput, KeywordInputError
from tg_jobs_searcher.telegram.client import ResolvedGroup


def message(text: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        text=text,
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="User"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("has_keywords", [True, False])
async def test_group_setup_reuses_shared_keywords_or_asks_for_first_list(has_keywords):
    owner = SimpleNamespace(id=uuid.uuid4())
    nonce = uuid.uuid4().hex
    # A legacy literal containing & must not be reparsed as a new AND rule.
    keywords = [KeywordInput("R&D", "r&d", ("r&d",))] if has_keywords else []
    groups = SimpleNamespace(
        find_active_by_telegram_id=AsyncMock(return_value=None),
        get_keywords=AsyncMock(return_value=keywords),
    )
    conversations = SimpleNamespace(set=AsyncMock())
    resolve = AsyncMock(return_value=ResolvedGroup(-100123, "Jobs", "jobs", 42, "Vacancies"))
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await _receive_group_reference(
            message("@jobs"), owner, {"nonce": nonce}, conversations, groups, resolve
        )
    _, step, draft = conversations.set.await_args.args
    assert draft["group"]["topic_id"] == 42
    assert draft["nonce"] == nonce
    assert "Vacancies" in answer.await_args.args[0]
    if has_keywords:
        assert step == ConversationStep.AWAITING_MODE
        assert draft["use_shared_keywords"] is True
        assert "keywords" not in draft  # Fetch current rules again when saving the group.
        assert "R&D" in answer.await_args.args[0]
        assert answer.await_args.kwargs["reply_markup"] is not None
    else:
        assert step == ConversationStep.AWAITING_KEYWORDS
        assert "во всех ваших группах" in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_first_list_keeps_and_or_syntax_and_explains_shared_scope():
    owner = SimpleNamespace(id=uuid.uuid4())
    conversations = SimpleNamespace(set=AsyncMock())
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await _receive_initial_keywords(
            message("python & remote, frontend"),
            owner,
            {"nonce": uuid.uuid4().hex},
            conversations,
        )
    _, step, draft = conversations.set.await_args.args
    assert step == ConversationStep.AWAITING_MODE
    assert [item["terms"] for item in draft["keywords"]] == [["python", "remote"], ["frontend"]]
    assert "для всех ваших групп" in answer.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("append", [True, False])
async def test_edit_from_group_card_confirms_change_for_all_groups(append):
    owner = SimpleNamespace(id=uuid.uuid4())
    group_id = uuid.uuid4()
    groups = SimpleNamespace(
        append_keywords=AsyncMock(return_value="card"),
        replace_keywords=AsyncMock(return_value="card"),
    )
    conversations = SimpleNamespace(clear=AsyncMock())
    step = (
        ConversationStep.AWAITING_APPEND_KEYWORDS
        if append
        else ConversationStep.AWAITING_REPLACE_KEYWORDS
    )
    with patch(
        "tg_jobs_searcher.bot.management_dialogs._send_card", new_callable=AsyncMock
    ) as send:
        await _receive_group_keywords(
            message("python"),
            owner,
            step.value,
            {"group_id": group_id.hex},
            conversations,
            groups,
        )
    changed = groups.append_keywords if append else groups.replace_keywords
    assert changed.await_args.args[:2] == (owner.id, group_id)
    assert "для всех ваших групп" in send.await_args.kwargs["prefix"]
    conversations.clear.assert_awaited_once_with(owner.id)


def test_merged_keyword_lists_can_be_deleted_in_pages():
    card = GroupCard(
        id=uuid.uuid4(),
        title="Jobs",
        telegram_chat_id=-100123,
        username=None,
        status=GroupStatus.ACTIVE,
        keywords=[KeywordSummary(uuid.uuid4(), f"rule{i}") for i in range(103)],
    )
    seen = []
    for page in range(6):
        keyboard = _keyword_selection_keyboard(card, page)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        assert len(buttons) <= 23
        assert all(len(button.callback_data) <= 64 for button in buttons)
        seen.extend(
            button.callback_data
            for button in buttons
            if button.callback_data.startswith("key:delete:")
        )
    assert seen == [f"key:delete:{k.id.hex}" for k in card.keywords]


@pytest.mark.asyncio
@pytest.mark.parametrize("emptied_during_setup", [False, True])
async def test_setup_confirmation_uses_current_shared_list(emptied_during_setup):
    owner = SimpleNamespace(id=uuid.uuid4())
    nonce = uuid.uuid4().hex
    draft = {
        "nonce": nonce,
        "use_shared_keywords": True,
        "group": {"telegram_chat_id": -100123, "title": "Jobs", "username": "jobs"},
    }
    conversations = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(step=ConversationStep.AWAITING_MODE.value, draft=draft)
        ),
        set=AsyncMock(),
        clear=AsyncMock(),
    )
    groups = SimpleNamespace(create_or_reactivate=AsyncMock(return_value="card"))
    if emptied_during_setup:
        groups.create_or_reactivate.side_effect = KeywordInputError("Empty shared list")
    with (
        patch("tg_jobs_searcher.bot.management.ConversationRepository", return_value=conversations),
        patch("tg_jobs_searcher.bot.management.GroupRepository", return_value=groups),
    ):
        router = create_management_router(
            admin_telegram_id=1,
            owners=SimpleNamespace(),
            session_factory=None,
            resolve_group=AsyncMock(),
            check_group_access=AsyncMock(),
        )
    callback = CallbackQuery(
        id="test",
        chat_instance="test",
        from_user=message("test").from_user,
        message=message("Mode"),
        data=f"setup:mode:{nonce}:new",
    )
    handler = next(
        h.callback
        for h in router.callback_query.handlers
        if h.callback.__name__ == "add_mode_callback"
    )
    with (
        patch(
            "tg_jobs_searcher.bot.management._ensure_owner_from_callback",
            AsyncMock(return_value=owner),
        ),
        patch("tg_jobs_searcher.bot.management._replace_with_card", AsyncMock()) as show_card,
        patch("tg_jobs_searcher.bot.management._replace_callback_text", AsyncMock()),
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
    ):
        await handler(callback)
    groups.create_or_reactivate.assert_awaited_once_with(
        owner.id,
        ResolvedGroup(-100123, "Jobs", "jobs"),
        None,
        scan_history=False,
    )
    if emptied_during_setup:
        _, step, next_draft = conversations.set.await_args.args
        assert step == ConversationStep.AWAITING_KEYWORDS
        assert "use_shared_keywords" not in next_draft
        show_card.assert_not_awaited()
    else:
        show_card.assert_awaited_once()
        conversations.clear.assert_awaited_once_with(owner.id)
