"""Persisted dialogs and callbacks for tracked-group configuration."""

from __future__ import annotations

import uuid

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.bot.handlers import AuthorizedUserMiddleware
from tg_jobs_searcher.bot.management_dialogs import (
    RULE_HELP,
    GroupResolver,
    _begin_keyword_dialog,
    _ensure_owner,
    _ensure_owner_from_callback,
    _receive_group_keywords,
    _receive_group_reference,
    _receive_initial_keywords,
    _send_feedback,
)
from tg_jobs_searcher.bot.management_state import (
    _callback_uuid,
    _keywords_from_draft,
    _parse_mode_callback,
    _resolved_group_from_draft,
)
from tg_jobs_searcher.bot.management_views import (
    _format_keyword_values,
    _format_scan_statuses,
    _mode_keyboard,
    _replace_callback_text,
    _replace_with_card,
    _replace_with_confirmation,
    _replace_with_group_list,
    _replace_with_keyword_selection,
    _send_group_list,
    _stale_callback,
)
from tg_jobs_searcher.db.conversation_repository import ConversationRepository
from tg_jobs_searcher.db.group_repository import GroupRepository
from tg_jobs_searcher.db.owner_repository import OwnerRepository
from tg_jobs_searcher.db.repository_types import ConversationStep
from tg_jobs_searcher.db.scan_repository import ScanRepository
from tg_jobs_searcher.telegram.client import GroupResolutionError


def create_management_router(
    *,
    admin_telegram_id: int,
    owners: OwnerRepository,
    session_factory: async_sessionmaker[AsyncSession],
    resolve_group: GroupResolver,
    check_group_access: GroupResolver,
) -> Router:
    router = Router(name="group_management")
    owner_repository = owners
    conversation_repository = ConversationRepository(session_factory)
    group_repository = GroupRepository(session_factory)
    scan_repository = ScanRepository(session_factory)
    owner_middleware = AuthorizedUserMiddleware(owner_repository)
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
            "Отправьте публичную ссылку на группу или канал, @username или числовой ID. "
            "Чтобы следить только за одной темой, отправьте ссылку на сообщение из неё "
            "(например, https://t.me/cyprusithr/46685).\n\n"
            "Если подключённый Telegram-аккаунт ещё не состоит в группе или не подписан на канал, "
            "он попробует вступить или подписаться по ссылке или @username. "
            "По одному числовому ID вступить или подписаться нельзя. "
            "В любой момент используйте /cancel."
        )

    @router.message(Command("cancel"))
    async def cancel_dialog(message: Message) -> None:
        owner = await _ensure_owner(owner_repository, message)
        cancelled = await conversation_repository.clear(owner.id)
        await message.answer("Текущий диалог отменён." if cancelled else "Нет активного диалога.")

    @router.message(Command("feedback"))
    async def feedback_command(message: Message, command: CommandObject, bot: Bot) -> None:
        owner = await _ensure_owner(owner_repository, message)
        if command.args and command.args.strip():
            await _send_feedback(message, bot, admin_telegram_id, command.args)
            return
        await conversation_repository.set(owner.id, ConversationStep.AWAITING_FEEDBACK, {})
        await message.answer(
            "Напишите одним сообщением идею, замечание или отзыв о боте. "
            "Я передам его администратору. До 3000 символов; для отмены — /cancel."
        )

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
            prompt=f"Введите дополнительные условия поиска.\n\n{RULE_HELP}",
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
            prompt=f"Введите новый полный список условий поиска.\n\n{RULE_HELP}",
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
            await callback.answer("У чата нет условий поиска.", show_alert=True)
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
        await callback.answer("Условие поиска удалено")
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
            await callback.answer("Для этого чата уже выполняется сканирование", show_alert=True)

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
            resolved_group = await check_group_access(reference)
        except GroupResolutionError:
            await callback.answer("Доступ к чату пока не восстановлен.", show_alert=True)
            return
        restored = await group_repository.restore_access(owner.id, group_id, resolved_group)
        if restored is None:
            await _stale_callback(callback)
            return
        await callback.answer("Доступ восстановлен, пропущенные сообщения проверяются")
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
        await callback.answer("Чат удалён")
        await _replace_callback_text(
            callback,
            "Чат удалён из мониторинга. Telegram-аккаунт остаётся в нём.",
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
            await callback.answer("Чат уже добавлен", show_alert=True)
            await _replace_with_card(callback, existing)
            return
        await conversation_repository.clear(owner.id)
        await callback.answer("Чат добавлен")
        scan_note = (
            "Настройка сканирования последних 7 дней сохранена."
            if mode == "history"
            else "Будут учитываться только новые сообщения."
        )
        await _replace_with_card(callback, card, prefix=f"Чат добавлен. {scan_note}\n\n")

    @router.message(F.text & ~F.text.startswith("/"))
    async def dialog_text(message: Message, bot: Bot) -> None:
        owner = await _ensure_owner(owner_repository, message)
        state = await conversation_repository.get(owner.id)
        if state is None:
            return
        if state.step == ConversationStep.AWAITING_FEEDBACK.value:
            if await _send_feedback(message, bot, admin_telegram_id, message.text or ""):
                await conversation_repository.clear(owner.id)
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

__all__ = [
    "RULE_HELP", "GroupResolver", "create_management_router",
    "_callback_uuid", "_format_keyword_values", "_format_scan_statuses",
    "_keywords_from_draft", "_mode_keyboard", "_parse_mode_callback",
]
