"""Group setup dialogs and feedback delivery."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from tg_jobs_searcher.bot.management_state import _callback_uuid, _draft_uuid
from tg_jobs_searcher.bot.management_views import (
    _format_keyword_values,
    _mode_keyboard,
    _send_card,
    _stale_callback,
)
from tg_jobs_searcher.db.conversation_repository import ConversationRepository
from tg_jobs_searcher.db.group_repository import GroupRepository
from tg_jobs_searcher.db.models import Owner
from tg_jobs_searcher.db.owner_repository import OwnerRepository
from tg_jobs_searcher.db.repository_types import ConversationStep
from tg_jobs_searcher.services.keywords import KeywordInputError, parse_keyword_input
from tg_jobs_searcher.telegram.client import GroupResolutionError, ResolvedGroup

GroupResolver = Callable[[str], Awaitable[ResolvedGroup]]

RULE_HELP = (
    "Запятая означает ИЛИ, знак & означает И внутри одного условия. "
    "Например: frontend & #вакансия, vue 3 & react 21 — пост подойдёт, "
    "если выполнится любая из двух пар.\n"
    "Фразы с пробелами пишите как обычно: vue 3. Если нужны буквальные & или запятая, "
    'заключите весь ключ в двойные кавычки: "R&D" & developer, "sales, marketing". '
    "При удалении удаляется условие целиком."
)


async def _send_feedback(message: Message, bot: Bot, admin_telegram_id: int, text: str) -> bool:
    feedback = text.strip()
    if not feedback:
        await message.answer("Напишите текст идеи или отзыва.")
        return False
    if len(feedback) > 3000:
        await message.answer("Сообщение слишком длинное. Сократите его до 3000 символов.")
        return False
    user = message.from_user
    assert user is not None
    name = user.full_name.replace("\n", " ")[:128]
    username = f"@{user.username}" if user.username else "не указан"
    try:
        await bot.send_message(
            admin_telegram_id,
            "Идея или отзыв о боте:\n"
            f"Пользователь: {name}\n"
            f"Username: {username}\n"
            f"Telegram ID: {user.id}\n\n{feedback}",
        )
    except TelegramAPIError:
        await message.answer("Не удалось передать отзыв. Попробуйте позже.")
        return False
    await message.answer("Спасибо! Сообщение отправлено администратору.")
    return True


async def _receive_group_reference(
    message: Message,
    owner: Owner,
    draft: dict[str, Any],
    conversations: ConversationRepository,
    groups: GroupRepository,
    resolve_group: GroupResolver,
) -> None:
    try:
        resolved_group = await resolve_group(message.text or "")
    except GroupResolutionError as exc:
        await message.answer(f"Не удалось добавить группу: {exc}\nПопробуйте ещё раз или /cancel.")
        return
    existing = await groups.find_active_by_telegram_id(owner.id, resolved_group.telegram_chat_id)
    if existing is not None:
        await conversations.clear(owner.id)
        await message.answer("Эта группа уже добавлена.")
        await _send_card(message, existing)
        return
    next_draft = {
        "nonce": draft.get("nonce", uuid.uuid4().hex),
        "group": {
            "telegram_chat_id": resolved_group.telegram_chat_id,
            "title": resolved_group.title,
            "username": resolved_group.username,
            "topic_id": resolved_group.topic_id,
            "topic_title": resolved_group.topic_title,
        },
    }
    await conversations.set(owner.id, ConversationStep.AWAITING_KEYWORDS, next_draft)
    await message.answer(
        f"Группа: {resolved_group.title}"
        + (f"\nТема: {resolved_group.topic_title}" if resolved_group.topic_id else "")
        + "\n\n"
        "Введите условия поиска.\n\n"
        f"{RULE_HELP}"
    )


async def _receive_initial_keywords(
    message: Message,
    owner: Owner,
    draft: dict[str, Any],
    conversations: ConversationRepository,
) -> None:
    try:
        keywords = parse_keyword_input(message.text or "")
    except KeywordInputError as exc:
        await message.answer(
            f"Не удалось сохранить условия поиска: {exc}\nПопробуйте ещё раз или /cancel."
        )
        return
    next_draft = {
        **draft,
        "keywords": [
            {
                "value": keyword.value,
                "normalized_value": keyword.normalized_value,
                "terms": list(keyword.terms),
            }
            for keyword in keywords
        ],
    }
    await conversations.set(owner.id, ConversationStep.AWAITING_MODE, next_draft)
    await message.answer(
        "Условия поиска:\n"
        f"{_format_keyword_values([keyword.value for keyword in keywords])}\n\n"
        "Что делать с историей сообщений?",
        reply_markup=_mode_keyboard(str(next_draft["nonce"])),
    )


async def _receive_group_keywords(
    message: Message,
    owner: Owner,
    step: str,
    draft: dict[str, Any],
    conversations: ConversationRepository,
    groups: GroupRepository,
) -> None:
    group_id = _draft_uuid(draft, "group_id")
    if group_id is None:
        await conversations.clear(owner.id)
        await message.answer("Диалог устарел. Откройте группу через /groups.")
        return
    try:
        keywords = parse_keyword_input(message.text or "")
    except KeywordInputError as exc:
        await message.answer(
            f"Не удалось сохранить условия поиска: {exc}\nПопробуйте ещё раз или /cancel."
        )
        return
    try:
        if step == ConversationStep.AWAITING_APPEND_KEYWORDS.value:
            card = await groups.append_keywords(owner.id, group_id, keywords)
        else:
            card = await groups.replace_keywords(owner.id, group_id, keywords)
    except KeywordInputError as exc:
        await message.answer(f"Не удалось сохранить условия поиска: {exc}")
        return
    await conversations.clear(owner.id)
    if card is None:
        await message.answer("Группа больше недоступна. Откройте /groups.")
        return
    await _send_card(message, card, prefix="Настройки сохранены.\n\n")


async def _begin_keyword_dialog(
    callback: CallbackQuery,
    owners: OwnerRepository,
    conversations: ConversationRepository,
    groups: GroupRepository,
    *,
    prefix: str,
    step: ConversationStep,
    prompt: str,
) -> None:
    owner = await _ensure_owner_from_callback(owners, callback)
    group_id = _callback_uuid(callback.data, prefix)
    if owner is None or group_id is None or await groups.get_card(owner.id, group_id) is None:
        await _stale_callback(callback)
        return
    await conversations.set(
        owner.id,
        step,
        {"nonce": uuid.uuid4().hex, "group_id": group_id.hex},
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(f"{prompt}\n\nДля отмены используйте /cancel.")


async def _ensure_owner(repository: OwnerRepository, message: Message) -> Owner:
    assert message.from_user is not None
    return await repository.ensure_owner(message.from_user.id, message.chat.id)


async def _ensure_owner_from_callback(
    repository: OwnerRepository, callback: CallbackQuery
) -> Owner | None:
    if not isinstance(callback.message, Message):
        return None
    return await repository.ensure_owner(callback.from_user.id, callback.message.chat.id)
