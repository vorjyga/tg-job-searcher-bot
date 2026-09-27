"""Administrator-only bot access management."""

from __future__ import annotations

from aiogram import BaseMiddleware, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, TelegramObject

from tg_jobs_searcher.db.repositories import OwnerRepository


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, admin_telegram_id: int) -> None:
        self._admin_telegram_id = admin_telegram_id

    async def __call__(self, handler, event: TelegramObject, data: dict):
        from_user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
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
    router.message.middleware(AdminOnlyMiddleware(admin_telegram_id))

    @router.message(Command("add_user"))
    async def add_user(message: Message, command: CommandObject) -> None:
        telegram_user_id = parse_user_id(command.args)
        if telegram_user_id is None:
            await message.answer("Формат: /add_user <числовой Telegram ID>")
            return
        if telegram_user_id == admin_telegram_id:
            await message.answer("У администратора уже есть доступ.")
            return
        created = await owners.grant_user(telegram_user_id)
        if created:
            await message.answer(
                f"Пользователь {telegram_user_id} добавлен. "
                "Попросите его открыть личный чат с ботом и отправить /start. "
                "До этого бот не сможет присылать ему уведомления."
            )
        else:
            await message.answer(f"Пользователь {telegram_user_id} уже добавлен.")

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

    @router.message(Command("users"))
    async def users(message: Message) -> None:
        users = await owners.list_users()
        if not users:
            await message.answer("Добавленных пользователей пока нет. Используйте /add_user <ID>.")
            return
        lines = ["Пользователи:"]
        for telegram_user_id, is_enabled in users:
            line = f"{telegram_user_id} — {'активен' if is_enabled else 'отключён'}"
            if len("\n".join([*lines, line])) > 3800:
                lines.append("… список обрезан")
                break
            lines.append(line)
        await message.answer("\n".join(lines))

    return router


def parse_user_id(value: str | None) -> int | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 19 or any(char not in "0123456789" for char in candidate):
        return None
    result = int(candidate)
    return result if 0 < result < 2**63 else None
