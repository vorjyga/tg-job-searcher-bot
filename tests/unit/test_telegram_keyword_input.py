"""Telegram formatting must not silently change saved search expressions."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import Chat, Message, MessageEntity

from tg_jobs_searcher.bot.catalog import save_catalog_keywords
from tg_jobs_searcher.bot.keyword_input import parse_keyword_message
from tg_jobs_searcher.bot.management_dialogs import (
    _receive_initial_keywords,
    _receive_personal_keywords,
    _receive_shared_keywords,
)
from tg_jobs_searcher.db.repository_types import ConversationStep
from tg_jobs_searcher.services.keywords import KeywordInputError
from tg_jobs_searcher.services.matching import find_matching_keywords, keywords_from_snapshot

SAFE_QUERY = (
    "(!#resume & !#резюме) & "
    "(angular, vue, react, front end, front-end, frontend, fullstack, full-stack, full stack)"
)


def message(text, entities=()):
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        text=text,
        entities=list(entities),
    )


def damaged_message():
    # The client has removed || around vue and attached a spoiler entity instead.
    return message(
        "!#resume & (angular vue react)",
        [MessageEntity(type="spoiler", offset=20, length=3)],
    )


def test_spoiler_is_rejected_with_instructions_for_preserving_query():
    with pytest.raises(KeywordInputError, match="спойлер") as error:
        parse_keyword_message(damaged_message())
    assert "запятые внутри скобок" in str(error.value)
    assert "моноширинный" in str(error.value)


@pytest.mark.parametrize("formatting", ["code", "pre"])
def test_code_query_retains_literal_or_operators(formatting):
    text = "!#resume & (angular || vue || react)"
    entity = MessageEntity(type=formatting, offset=0, length=len(text))
    rules = parse_keyword_message(message(text, [entity]))
    assert rules[0].value == text
    assert rules[0].expression is not None


def test_hashtags_do_not_interfere_with_query_parsing():
    msg = message(SAFE_QUERY, [MessageEntity(type="hashtag", offset=2, length=7)])
    assert parse_keyword_message(msg) == parse_keyword_message(message(SAFE_QUERY))


@pytest.mark.parametrize(
    "text, expected",
    [
        ("React role", True),
        ("front end developer", True),
        ("full-stack role", True),
        ("#resume React", False),
        ("Full stack #резюме", False),
        ("backend role", False),
    ],
)
def test_comma_query_matches_users_intended_logic(text, expected):
    keywords = keywords_from_snapshot(
        [rule.snapshot() for rule in parse_keyword_message(message(SAFE_QUERY))]
    )
    assert bool(find_matching_keywords(text, keywords)) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["initial", "personal", "append", "replace", "catalog"])
async def test_spoiler_never_changes_settings_or_clears_dialog(mode):
    msg = damaged_message()
    owner = SimpleNamespace(id=uuid.uuid4())
    groups = SimpleNamespace(
        append_shared_keywords=AsyncMock(),
        replace_shared_keywords=AsyncMock(),
    )
    conversations = SimpleNamespace(set=AsyncMock(), clear=AsyncMock())
    catalog = SimpleNamespace(set_search=AsyncMock())
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        if mode == "initial":
            await _receive_initial_keywords(msg, owner, {}, conversations)
        elif mode == "personal":
            await _receive_personal_keywords(msg, owner, conversations, groups)
        elif mode == "catalog":
            await save_catalog_keywords(msg, owner.id, catalog, conversations, msg.text)
        else:
            step = (
                ConversationStep.AWAITING_APPEND_KEYWORDS
                if mode == "append"
                else ConversationStep.AWAITING_REPLACE_KEYWORDS
            )
            await _receive_shared_keywords(msg, owner, step.value, conversations, groups)
    groups.append_shared_keywords.assert_not_awaited()
    groups.replace_shared_keywords.assert_not_awaited()
    catalog.set_search.assert_not_awaited()
    conversations.set.assert_not_awaited()
    conversations.clear.assert_not_awaited()
    assert "спойлер" in answer.await_args.args[0]


def test_catalog_command_arguments_also_check_spoiler_entities():
    text = "/search !#resume & (angular vue react)"
    msg = message(text, [MessageEntity(type="spoiler", offset=28, length=3)])
    with pytest.raises(KeywordInputError, match="спойлер"):
        parse_keyword_message(msg, text.split(maxsplit=1)[1])
