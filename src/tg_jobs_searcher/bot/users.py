"""Administrator-only bot access management."""

from __future__ import annotations

import logging

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, TelegramObject

from tg_jobs_searcher.db.repositories import OwnerRepository

logger = logging.getLogger(__name__)
APPROVE_CALLBACK_PREFIX = "access:approve:"


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, admin_telegram_id: int) -> None:
        self._admin_telegram_id = admin_telegram_id

    async def __call__(self, handler, event: TelegramObject, data: dict):
        from_user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        if chat is None:
            chat = getattr(getattr(event, "message", None), "chat", None)
        if (
            from_user is None
            or from_user.id != self._admin_telegram_id
            or chat is None
            or chat.type != "private"
        ):
            return None
        return await handler(event, data)


def create_admin_router(admin_telegram_id: int, owners: OwnerRepository) -> Router:
    router = Router(name="admin_users")
    admin_middleware = AdminOnlyMiddleware(admin_telegram_id)
    router.message.middleware(admin_middleware)
    router.callback_query.middleware(admin_middleware)

    @router.message(Command("add_user"))
    async def add_user(message: Message, command: CommandObject, bot: Bot) -> None:
        telegram_user_id = parse_user_id(command.args)
        if telegram_user_id is None:
            await message.answer("Формат: /add_user <числовой Telegram ID>")
            return
        if telegram_user_id == admin_telegram_id:
            await message.answer("У администратора уже есть доступ.")
            return
        await message.answer(await grant_user_and_notify(owners, bot, telegram_user_id))

    @router.callback_query(F.data.startswith(APPROVE_CALLBACK_PREFIX))
    async def approve_user(callback: CallbackQuery, bot: Bot) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer("Заявка недоступна.", show_alert=True)
            return
        telegram_user_id = parse_user_id((callback.data or "")[len(APPROVE_CALLBACK_PREFIX):])
        if telegram_user_id is None or telegram_user_id == admin_telegram_id:
            await callback.answer("Некорректная заявка.", show_alert=True)
            return
        result = await grant_user_and_notify(owners, bot, telegram_user_id)
        await callback.answer(result[:200], show_alert=True)
        try:
            await callback.message.edit_text(
                f"{callback.message.text or 'Запрос доступа'}\n\n{result}",
                reply_markup=None,
            )
        except TelegramBadRequest:
            logger.warning("approved_invite_message_edit_failed")

    @router.message(Command("remove_user"))
    async def remove_user(message: Message, command: CommandObject) -> None:
        telegram_user_id = parse_user_id(command.args)
        if telegram_user_id is None:
            await message.answer("Формат: /remove_user <числовой Telegram ID>")
            return
        if telegram_user_id == admin_telegram_id:
            await message.answer("Нельзя удалить администратора.")
            return
        if await owners.revoke_user(telegram_user_id):
            await message.answer(
                f"Доступ пользователя {telegram_user_id} закрыт. "
                "Его группы сохранены, но мониторинг и уведомления остановлены."
            )
        else:
            await message.answer(f"У пользователя {telegram_user_id} нет активного доступа.")

    @router.message(Command("users", "list_users"))
    async def users(message: Message) -> None:
        users = await owners.list_users()
        active = [telegram_user_id for telegram_user_id, enabled in users if enabled]
        disabled = [telegram_user_id for telegram_user_id, enabled in users if not enabled]
        lines = [f"Активные пользователи ({len(active) + 1}):", f"• {admin_telegram_id} — администратор"]
        for telegram_user_id in active:
            lines.append(f"• {telegram_user_id}")
        if disabled:
            lines.append(f"\nОтключённые ({len(disabled)}):")
            for telegram_user_id in disabled:
                lines.append(f"• {telegram_user_id}")
        chunk: list[str] = []
        for line in lines:
            if chunk and len("\n".join([*chunk, line])) > 3800:
                await message.answer("\n".join(chunk))
                chunk = []
            chunk.append(line)
        if chunk:
            await message.answer("\n".join(chunk))

    return router


async def grant_user_and_notify(owners: OwnerRepository, bot: Bot, telegram_user_id: int) -> str:
    """Grant access once and tell an applicant when Telegram allows delivery."""
    if not await owners.grant_user(telegram_user_id):
        return f"Пользователь {telegram_user_id} уже добавлен."
    try:
        await bot.send_message(
            telegram_user_id,
            "Вам открыт доступ к боту. Теперь можно добавлять группы для мониторинга "
            "командой /add. Список команд — /start.",
        )
    except TelegramAPIError:
        logger.warning("access_granted_but_notification_failed", exc_info=True)
        return (
            f"Пользователь {telegram_user_id} добавлен, но уведомление ему не доставлено. "
            "Попросите его открыть личный чат с ботом и отправить /start."
        )
    return f"Пользователь {telegram_user_id} добавлен и получил уведомление."


def parse_user_id(value: str | None) -> int | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 19 or any(char not in "0123456789" for char in candidate):
        return None
    result = int(candidate)
    return result if 0 < result < 2**63 else None
