"""Bot responses and keyboards for group management."""

from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from tg_jobs_searcher.db.models import GroupStatus, ScanJobType, WorkStatus
from tg_jobs_searcher.db.repository_types import GroupCard, GroupSummary, ScanJobStatus


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
        f"{card.title}\n\nВыберите условие для удаления:",
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
    builder.button(text="Добавить условия", callback_data=f"group:add_keys:{card.id.hex}")
    builder.button(text="Заменить все условия", callback_data=f"group:replace_keys:{card.id.hex}")
    if card.keywords:
        builder.button(text="Удалить условия", callback_data=f"group:remove_keys:{card.id.hex}")
    if card.status == GroupStatus.ACTIVE:
        builder.button(
            text="Повторить поиск за 7 дней", callback_data=f"group:rescan:{card.id.hex}"
        )
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
    topic_line = (
        f"Тема: {card.topic_title or card.topic_id} (ID {card.topic_id})\n"
        if card.topic_id is not None
        else ""
    )
    return (
        f"{card.title}\n"
        f"ID: {card.telegram_chat_id}\n"
        f"{topic_line}"
        f"Статус: {_status_label(card.status)}\n"
        f"Условия поиска:\n{keywords}"
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
