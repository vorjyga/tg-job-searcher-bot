"""Owner-only aiogram command handlers used during stage 2."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram import BaseMiddleware, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message, TelegramObject

from tg_jobs_searcher.telegram.client import (
    GroupResolutionError,
    ResolvedGroup,
    list_accessible_groups,
    resolve_accessible_group,
)

logger = logging.getLogger(__name__)

GroupResolver = Callable[[str], Awaitable[ResolvedGroup]]
GroupLister = Callable[[], Awaitable[list[ResolvedGroup]]]
OwnerRegistrar = Callable[[int, int], Awaitable[object]]


class OwnerOnlyMiddleware(BaseMiddleware):
    """Silently discard every update not sent by the configured owner."""

    def __init__(self, owner_telegram_id: int) -> None:
        self._owner_telegram_id = owner_telegram_id

    async def __call__(self, handler, event: TelegramObject, data: dict):
        from_user = getattr(event, "from_user", None)
        if from_user is None or from_user.id != self._owner_telegram_id:
            logger.warning("unauthorised_bot_update_ignored")
            return None
        return await handler(event, data)


def create_router(
    *,
    owner_telegram_id: int,
    resolve_group: GroupResolver,
    list_groups: GroupLister,
    register_owner: OwnerRegistrar | None = None,
) -> Router:
    router = Router(name="owner_commands")
    router.message.middleware(OwnerOnlyMiddleware(owner_telegram_id))

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if register_owner is not None and message.from_user is not None:
            await register_owner(message.from_user.id, message.chat.id)
        await message.answer(
            "Бот подключён к вашему Telegram-аккаунту.\n\n"
            "Пока доступны команды:\n"
            "/help — справка\n"
            "/available_groups — показать доступные группы\n"
            "/check_group <ссылка, @username или ID> — проверить доступ к группе\n"
            "/add — добавить группу для мониторинга\n"
            "/groups — управлять добавленными группами"
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Бот принимает команды только от настроенного владельца.\n\n"
            "Подключённый Telegram-аккаунт должен уже состоять в группе. "
            "Используйте публичную ссылку, @username или числовой ID. "
            "Пригласительные ссылки не используются для вступления в группы.\n\n"
            "/add — добавить группу, /groups — изменить её ключевые слова, "
            "/cancel — отменить текущий диалог."
        )

    @router.message(Command("available_groups"))
    async def available_groups_command(message: Message) -> None:
        groups = await list_groups()
        if not groups:
            await message.answer("У подключённого аккаунта нет доступных групп.")
            return
        await message.answer(format_groups(groups))

    @router.message(Command("check_group"))
    async def check_group_command(message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Формат: /check_group <ссылка, @username или ID>")
            return
        try:
            group = await resolve_group(command.args)
        except GroupResolutionError as exc:
            await message.answer(f"Группа недоступна: {exc}")
            return
        await message.answer(
            "Доступ подтверждён:\n"
            f"{group.title}\n"
            f"ID: {group.telegram_chat_id}\n"
            f"Username: {group.username or 'нет'}"
        )

    return router


def make_telegram_group_resolver(client) -> GroupResolver:
    async def resolver(reference: str) -> ResolvedGroup:
        return await resolve_accessible_group(client, reference)

    return resolver


def make_telegram_group_lister(client) -> GroupLister:
    async def lister() -> list[ResolvedGroup]:
        return await list_accessible_groups(client)

    return lister


def format_groups(groups: list[ResolvedGroup], limit: int = 50) -> str:
    lines = ["Доступные группы:"]
    displayed_count = 0
    for group in groups[:limit]:
        title = group.title.replace("\n", " ")[:120]
        line = f"• {title} — {group.telegram_chat_id}"
        if len("\n".join([*lines, line])) > 3800:
            break
        lines.append(line)
        displayed_count += 1
    hidden_count = len(groups) - displayed_count
    if hidden_count:
        lines.append(f"… и ещё {hidden_count}")
    return "\n".join(lines)
