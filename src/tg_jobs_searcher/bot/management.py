"""Persisted dialogs and callbacks for tracked-group configuration."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.bot.handlers import OwnerOnlyMiddleware
from tg_jobs_searcher.db.models import GroupStatus, Owner, ScanJobType, WorkStatus
from tg_jobs_searcher.db.repositories import (
    ConversationRepository,
    ConversationStep,
    GroupCard,
    GroupRepository,
    GroupSummary,
    OwnerRepository,
    ScanJobStatus,
    ScanRepository,
)
from tg_jobs_searcher.services.keywords import KeywordInput, KeywordInputError, parse_keyword_input
from tg_jobs_searcher.telegram.client import GroupResolutionError, ResolvedGroup

GroupResolver = Callable[[str], Awaitable[ResolvedGroup]]


def create_management_router(
    *,
    owner_telegram_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    resolve_group: GroupResolver,
) -> Router:
    router = Router(name="group_management")
    owner_repository = OwnerRepository(session_factory)
    conversation_repository = ConversationRepository(session_factory)
    group_repository = GroupRepository(session_factory)
    scan_repository = ScanRepository(session_factory)
    owner_middleware = OwnerOnlyMiddleware(owner_telegram_id)
    router.message.middleware(owner_middleware)
    router.callback_query.middleware(owner_middleware)

    @router.message(Command("add"))
    async def add_group(message: Message) -> None:
        owner = await _ensure_owner(owner_repository, message)
        await conversation_repository.set(
            owner.id,
            ConversationStep.AWAITING_GROUP,
            {"nonce": uuid.uuid4().hex},
        )
        await message.answer(
            "Отправьте публичную ссылку на группу, @username или числовой ID.\n\n"
            "Подключённый Telegram-аккаунт уже должен состоять в этой группе. "
            "В любой момент используйте /cancel."
        )

    @router.message(Command("cancel"))
    async def cancel_dialog(message: Message) -> None:
        owner = await _ensure_owner(owner_repository, message)
        cancelled = await conversation_repository.clear(owner.id)
        await message.answer("Текущий диалог отменён." if cancelled else "Нет активного диалога.")

    @router.message(Command("groups"))
    async def groups_command(message: Message) -> None:
        owner = await _ensure_owner(owner_repository, message)
        await _send_group_list(message, await group_repository.list_groups(owner.id))

    @router.message(Command("status"))
    async def status_command(message: Message) -> None:
        owner = await _ensure_owner(owner_repository, message)
        await message.answer(_format_scan_statuses(await scan_repository.status_for_owner(owner.id)))

    @router.callback_query(F.data == "groups:list")
    async def groups_list_callback(callback: CallbackQuery) -> None:
        owner = await _ensure_owner_from_callback(owner_repository, callback)
        if owner is None:
            await callback.answer("Эта кнопка больше недоступна.", show_alert=True)
            return
        summaries = await group_repository.list_groups(owner.id)
        await callback.answer()
        await _replace_with_group_list(callback, summaries)

    @router.callback_query(F.data == "groups:add_hint")
    async def add_hint_callback(callback: CallbackQuery) -> None:
        await callback.answer("Используйте команду /add", show_alert=True)

    @router.callback_query(F.data.startswith("group:open:"))
    async def open_group_callback(callback: CallbackQuery) -> None:
        owner = await _ensure_owner_from_callback(owner_repository, callback)
        group_id = _callback_uuid(callback.data, "group:open:")
        if owner is None or group_id is None:
            await _stale_callback(callback)
            return
        card = await group_repository.get_card(owner.id, group_id)
        if card is None:
            await _stale_callback(callback)
            return
        await callback.answer()
        await _replace_with_card(callback, card)

    @router.callback_query(F.data.startswith("group:add_keys:"))
    async def append_keys_callback(callback: CallbackQuery) -> None:
        await _begin_keyword_dialog(
            callback,
            owner_repository,
            conversation_repository,
            group_repository,
            prefix="group:add_keys:",
            step=ConversationStep.AWAITING_APPEND_KEYWORDS,
            prompt="Введите дополнительные ключевые слова или фразы через запятую.",
        )

    @router.callback_query(F.data.startswith("group:replace_keys:"))
    async def replace_keys_callback(callback: CallbackQuery) -> None:
        await _begin_keyword_dialog(
            callback,
            owner_repository,
            conversation_repository,
            group_repository,
            prefix="group:replace_keys:",
            step=ConversationStep.AWAITING_REPLACE_KEYWORDS,
            prompt="Введите новый полный список ключевых слов или фраз через запятую.",
        )

    @router.callback_query(F.data.startswith("group:remove_keys:"))
    async def remove_keys_callback(callback: CallbackQuery) -> None:
        owner = await _ensure_owner_from_callback(owner_repository, callback)
        group_id = _callback_uuid(callback.data, "group:remove_keys:")
        if owner is None or group_id is None:
            await _stale_callback(callback)
            return
        card = await group_repository.get_card(owner.id, group_id)
        if card is None:
            await _stale_callback(callback)
            return
        if not card.keywords:
            await callback.answer("У группы нет ключевых слов.", show_alert=True)
            return
        await callback.answer()
        await _replace_with_keyword_selection(callback, card)

    @router.callback_query(F.data.startswith("key:delete:"))
    async def delete_key_callback(callback: CallbackQuery) -> None:
        owner = await _ensure_owner_from_callback(owner_repository, callback)
        keyword_id = _callback_uuid(callback.data, "key:delete:")
        if owner is None or keyword_id is None:
            await _stale_callback(callback)
            return
        card = await group_repository.delete_keyword(owner.id, keyword_id)
        if card is None:
            await _stale_callback(callback)
            return
        await callback.answer("Ключевое слово удалено")
        await _replace_with_card(callback, card)

    @router.callback_query(F.data.startswith("group:rescan:"))
    async def rescan_group_callback(callback: CallbackQuery) -> None:
        owner = await _ensure_owner_from_callback(owner_repository, callback)
        group_id = _callback_uuid(callback.data, "group:rescan:")
        if owner is None or group_id is None:
            await _stale_callback(callback)
            return
        job, created = await scan_repository.create_manual_scan(owner.id, group_id)
        if job is None:
            await _stale_callback(callback)
            return
        if created:
            await callback.answer("Сканирование последних 7 дней поставлено в очередь")
        else:
            await callback.answer("Для этой группы уже выполняется сканирование", show_alert=True)

    @router.callback_query(F.data.startswith("group:check_access:"))
    async def check_access_callback(callback: CallbackQuery) -> None:
        owner = await _ensure_owner_from_callback(owner_repository, callback)
        group_id = _callback_uuid(callback.data, "group:check_access:")
        if owner is None or group_id is None:
            await _stale_callback(callback)
            return
        card = await group_repository.get_card(owner.id, group_id)
        if card is None:
            await _stale_callback(callback)
            return
        reference = f"@{card.username}" if card.username else str(card.telegram_chat_id)
        try:
            resolved_group = await resolve_group(reference)
        except GroupResolutionError:
            await callback.answer("Доступ к группе пока не восстановлен.", show_alert=True)
            return
        restored = await group_repository.restore_access(owner.id, group_id, resolved_group)
        if restored is None:
            await _stale_callback(callback)
            return
        await callback.answer("Доступ восстановлен, мониторинг продолжен")
        await _replace_with_card(callback, restored)

    @router.callback_query(F.data.startswith("group:remove:"))
    async def remove_group_callback(callback: CallbackQuery) -> None:
        owner = await _ensure_owner_from_callback(owner_repository, callback)
        group_id = _callback_uuid(callback.data, "group:remove:")
        if owner is None or group_id is None:
            await _stale_callback(callback)
            return
        card = await group_repository.get_card(owner.id, group_id)
        if card is None:
            await _stale_callback(callback)
            return
        await callback.answer()
        await _replace_with_confirmation(callback, card)

    @router.callback_query(F.data.startswith("group:confirm_remove:"))
    async def confirm_remove_group_callback(callback: CallbackQuery) -> None:
        owner = await _ensure_owner_from_callback(owner_repository, callback)
        group_id = _callback_uuid(callback.data, "group:confirm_remove:")
        if owner is None or group_id is None:
            await _stale_callback(callback)
            return
        if not await group_repository.remove_group(owner.id, group_id):
            await _stale_callback(callback)
            return
        await callback.answer("Группа удалена")
        await _replace_callback_text(
            callback,
            "Группа удалена из мониторинга. Telegram-аккаунт остаётся её участником.",
            groups_keyboard=True,
        )

    @router.callback_query(F.data.startswith("setup:mode:"))
    async def add_mode_callback(callback: CallbackQuery) -> None:
        owner = await _ensure_owner_from_callback(owner_repository, callback)
        parsed = _parse_mode_callback(callback.data)
        if owner is None or parsed is None:
            await _stale_callback(callback)
            return
        nonce, mode = parsed
        state = await conversation_repository.get(owner.id)
        if (
            state is None
            or state.step != ConversationStep.AWAITING_MODE.value
            or state.draft.get("nonce") != nonce
        ):
            await _stale_callback(callback)
            return
        try:
            resolved_group = _resolved_group_from_draft(state.draft)
            keywords = _keywords_from_draft(state.draft)
        except (KeyError, TypeError, ValueError):
            await conversation_repository.clear(owner.id)
            await _stale_callback(callback)
            return
        try:
            card = await group_repository.create_or_reactivate(
                owner.id,
                resolved_group,
                keywords,
                scan_history=mode == "history",
            )
        except ValueError:
            existing = await group_repository.find_active_by_telegram_id(
                owner.id, resolved_group.telegram_chat_id
            )
            if existing is None:
                raise
            await conversation_repository.clear(owner.id)
            await callback.answer("Группа уже добавлена", show_alert=True)
            await _replace_with_card(callback, existing)
            return
        await conversation_repository.clear(owner.id)
        await callback.answer("Группа добавлена")
        scan_note = (
            "Настройка сканирования последних 7 дней сохранена."
            if mode == "history"
            else "Будут учитываться только новые сообщения."
        )
        await _replace_with_card(callback, card, prefix=f"Группа добавлена. {scan_note}\n\n")

    @router.message(F.text & ~F.text.startswith("/"))
    async def dialog_text(message: Message) -> None:
        owner = await _ensure_owner(owner_repository, message)
        state = await conversation_repository.get(owner.id)
        if state is None:
            return
        if state.step == ConversationStep.AWAITING_GROUP.value:
            await _receive_group_reference(
                message,
                owner,
                state.draft,
                conversation_repository,
                group_repository,
                resolve_group,
            )
            return
        if state.step == ConversationStep.AWAITING_KEYWORDS.value:
            await _receive_initial_keywords(message, owner, state.draft, conversation_repository)
            return
        if state.step == ConversationStep.AWAITING_MODE.value:
            await message.answer(
                "Выберите один из режимов кнопкой в предыдущем сообщении или /cancel."
            )
            return
        if state.step in {
            ConversationStep.AWAITING_APPEND_KEYWORDS.value,
            ConversationStep.AWAITING_REPLACE_KEYWORDS.value,
        }:
            await _receive_group_keywords(
                message,
                owner,
                state.step,
                state.draft,
                conversation_repository,
                group_repository,
            )

    return router


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
        },
    }
    await conversations.set(owner.id, ConversationStep.AWAITING_KEYWORDS, next_draft)
    await message.answer(
        f"Группа: {resolved_group.title}\n\n"
        "Введите ключевые слова и фразы через запятую. Например:\n"
        "python, backend developer, #вакансия"
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
            f"Не удалось сохранить ключевые слова: {exc}\nПопробуйте ещё раз или /cancel."
        )
        return
    next_draft = {
        **draft,
        "keywords": [
            {"value": keyword.value, "normalized_value": keyword.normalized_value}
            for keyword in keywords
        ],
    }
    await conversations.set(owner.id, ConversationStep.AWAITING_MODE, next_draft)
    await message.answer(
        "Ключевые слова:\n"
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
            f"Не удалось сохранить ключевые слова: {exc}\nПопробуйте ещё раз или /cancel."
        )
        return
    if step == ConversationStep.AWAITING_APPEND_KEYWORDS.value:
        card = await groups.append_keywords(owner.id, group_id, keywords)
    else:
        card = await groups.replace_keywords(owner.id, group_id, keywords)
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


async def _send_group_list(message: Message, groups: list[GroupSummary]) -> None:
    if not groups:
        await message.answer("Нет добавленных групп. Используйте /add.")
        return
    await message.answer("Выберите группу:", reply_markup=_group_list_keyboard(groups))


async def _replace_with_group_list(callback: CallbackQuery, groups: list[GroupSummary]) -> None:
    if not isinstance(callback.message, Message):
        return
    if not groups:
        await _replace_callback_text(callback, "Нет добавленных групп. Используйте /add.")
        return
    await _replace_callback_text(callback, "Выберите группу:", _group_list_keyboard(groups))


async def _send_card(message: Message, card: GroupCard, prefix: str = "") -> None:
    await message.answer(prefix + _card_text(card), reply_markup=_card_keyboard(card))


async def _replace_with_card(callback: CallbackQuery, card: GroupCard, prefix: str = "") -> None:
    await _replace_callback_text(callback, prefix + _card_text(card), _card_keyboard(card))


async def _replace_with_keyword_selection(callback: CallbackQuery, card: GroupCard) -> None:
    await _replace_callback_text(
        callback,
        f"{card.title}\n\nВыберите ключевое слово для удаления:",
        _keyword_selection_keyboard(card),
    )


async def _replace_with_confirmation(callback: CallbackQuery, card: GroupCard) -> None:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, удалить группу", callback_data=f"group:confirm_remove:{card.id.hex}")
    builder.button(text="Отмена", callback_data=f"group:open:{card.id.hex}")
    builder.adjust(1)
    await _replace_callback_text(
        callback,
        f"Удалить «{card.title}» из мониторинга? Аккаунт Telegram останется участником группы.",
        builder.as_markup(),
    )


async def _replace_callback_text(
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
    *,
    groups_keyboard: bool = False,
) -> None:
    if not isinstance(callback.message, Message):
        return
    if groups_keyboard:
        builder = InlineKeyboardBuilder()
        builder.button(text="К списку групп", callback_data="groups:list")
        reply_markup = builder.as_markup()
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _stale_callback(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка устарела. Откройте /groups.", show_alert=True)


def _group_list_keyboard(groups: list[GroupSummary]):
    builder = InlineKeyboardBuilder()
    for group in groups:
        builder.button(
            text=f"{_status_label(group.status)} · {group.title[:45]}",
            callback_data=f"group:open:{group.id.hex}",
        )
    builder.button(text="Добавить группу", callback_data="groups:add_hint")
    builder.adjust(1)
    return builder.as_markup()


def _card_keyboard(card: GroupCard):
    builder = InlineKeyboardBuilder()
    builder.button(text="Добавить ключи", callback_data=f"group:add_keys:{card.id.hex}")
    builder.button(text="Заменить все ключи", callback_data=f"group:replace_keys:{card.id.hex}")
    if card.keywords:
        builder.button(text="Удалить ключи", callback_data=f"group:remove_keys:{card.id.hex}")
    if card.status == GroupStatus.ACTIVE:
        builder.button(text="Повторить поиск за 7 дней", callback_data=f"group:rescan:{card.id.hex}")
    if card.status == GroupStatus.ACCESS_LOST:
        builder.button(text="Проверить доступ", callback_data=f"group:check_access:{card.id.hex}")
    builder.button(text="Удалить группу", callback_data=f"group:remove:{card.id.hex}")
    builder.button(text="К списку групп", callback_data="groups:list")
    builder.adjust(1)
    return builder.as_markup()


def _keyword_selection_keyboard(card: GroupCard):
    builder = InlineKeyboardBuilder()
    for keyword in card.keywords:
        builder.button(
            text=f"Удалить: {keyword.value[:40]}", callback_data=f"key:delete:{keyword.id.hex}"
        )
    builder.button(text="Назад", callback_data=f"group:open:{card.id.hex}")
    builder.adjust(1)
    return builder.as_markup()


def _mode_keyboard(nonce: str):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Найти за последние 7 дней и следить",
        callback_data=f"setup:mode:{nonce}:history",
    )
    builder.button(
        text="Только следить за новыми",
        callback_data=f"setup:mode:{nonce}:new",
    )
    builder.adjust(1)
    return builder.as_markup()


def _card_text(card: GroupCard) -> str:
    keywords = _format_keyword_values([keyword.value for keyword in card.keywords])
    return (
        f"{card.title}\n"
        f"ID: {card.telegram_chat_id}\n"
        f"Статус: {_status_label(card.status)}\n"
        f"Ключевые слова:\n{keywords}"
    )


def _format_keyword_values(values: list[str]) -> str:
    if not values:
        return "—"
    lines: list[str] = []
    for value in values[:30]:
        line = f"• {value.replace(chr(10), ' ')[:120]}"
        if len("\n".join([*lines, line])) > 3200:
            break
        lines.append(line)
    if len(values) > len(lines):
        lines.append(f"… и ещё {len(values) - len(lines)}")
    return "\n".join(lines)


def _status_label(status: GroupStatus) -> str:
    return {
        GroupStatus.ACTIVE: "активна",
        GroupStatus.PAUSED: "приостановлена",
        GroupStatus.ACCESS_LOST: "нет доступа",
        GroupStatus.REMOVED: "удалена",
    }[status]


def _format_scan_statuses(statuses: list[ScanJobStatus]) -> str:
    if not statuses:
        return "Заданий сканирования пока нет."
    lines = ["Статус сканирований:"]
    for item in statuses:
        kind = {
            ScanJobType.INITIAL_SEVEN_DAYS: "первичный поиск",
            ScanJobType.MANUAL_SEVEN_DAYS: "повторный поиск",
            ScanJobType.RECOVERY: "восстановление",
        }[item.job_type]
        state = {
            WorkStatus.PENDING: "ожидает",
            WorkStatus.RUNNING: "выполняется",
            WorkStatus.RETRY: "будет повторено",
            WorkStatus.COMPLETED: "завершено",
            WorkStatus.FAILED: "ошибка",
            WorkStatus.CANCELLED: "отменено",
        }[item.status]
        lines.append(
            f"• {item.group_title[:80]} — {kind}: {state}; "
            f"проверено {item.messages_checked}, совпадений {item.matches_found}"
        )
    return "\n".join(lines)[:4096]


def _callback_uuid(value: str | None, prefix: str) -> uuid.UUID | None:
    if value is None or not value.startswith(prefix):
        return None
    try:
        return uuid.UUID(hex=value.removeprefix(prefix))
    except ValueError:
        return None


def _draft_uuid(draft: dict[str, Any], key: str) -> uuid.UUID | None:
    value = draft.get(key)
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(hex=value)
    except ValueError:
        return None


def _parse_mode_callback(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    parts = value.split(":")
    if len(parts) != 4 or parts[:2] != ["setup", "mode"] or parts[3] not in {"history", "new"}:
        return None
    nonce = parts[2]
    if len(nonce) != 32 or not nonce.isalnum():
        return None
    return nonce, parts[3]


def _resolved_group_from_draft(draft: dict[str, Any]) -> ResolvedGroup:
    group = draft["group"]
    if not isinstance(group, dict):
        raise ValueError("Invalid group draft")
    chat_id = group["telegram_chat_id"]
    title = group["title"]
    username = group.get("username")
    if not isinstance(chat_id, int) or not isinstance(title, str):
        raise ValueError("Invalid group draft")
    if username is not None and not isinstance(username, str):
        raise ValueError("Invalid group draft")
    return ResolvedGroup(telegram_chat_id=chat_id, title=title, username=username)


def _keywords_from_draft(draft: dict[str, Any]) -> list[KeywordInput]:
    raw_keywords = draft["keywords"]
    if not isinstance(raw_keywords, list):
        raise ValueError("Invalid keyword draft")
    keywords: list[KeywordInput] = []
    for item in raw_keywords:
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            raise ValueError("Invalid keyword draft")
        parsed = parse_keyword_input(item["value"])
        if len(parsed) != 1:
            raise ValueError("Invalid keyword draft")
        keywords.append(parsed[0])
    if not keywords:
        raise ValueError("Invalid keyword draft")
    return keywords
