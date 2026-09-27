"""Public onboarding and commands for active users."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from tg_jobs_searcher.bot.users import APPROVE_CALLBACK_PREFIX
from tg_jobs_searcher.db.owner_repository import OwnerRepository
from tg_jobs_searcher.telegram.client import (
    GroupResolutionError,
    ResolvedGroup,
    list_accessible_groups,
    resolve_accessible_group,
    resolve_or_join_group,
)

logger = logging.getLogger(__name__)

GroupResolver = Callable[[str], Awaitable[ResolvedGroup]]
GroupLister = Callable[[], Awaitable[list[ResolvedGroup]]]
INVITE_CALLBACK_DATA = "access:request_invite"
INVITE_COOLDOWN = timedelta(hours=1)


class AuthorizedUserMiddleware(BaseMiddleware):
    """Accept private-chat updates unless the administrator disabled the user."""

    def __init__(self, owners: OwnerRepository) -> None:
        self._owners = owners

    async def __call__(self, handler, event: TelegramObject, data: dict):
        from_user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        if chat is None:
            message = getattr(event, "message", None)
            chat = getattr(message, "chat", None)
        if (
            from_user is None
            or chat is None
            or chat.type != "private"
            or not await self._owners.is_authorized(from_user.id)
        ):
            logger.warning("unauthorised_bot_update_ignored")
            return None
        return await handler(event, data)


def create_public_router(
    *,
    admin_telegram_id: int,
    owners: OwnerRepository,
) -> Router:
    """Register public /start and observe private-chat block transitions."""
    router = Router(name="public_access")
    last_invite_request: dict[int, datetime] = {}
    invite_request_lock = asyncio.Lock()

    async def answer_access_denied(message: Message, user_id: int) -> None:
        if await owners.is_disabled(user_id):
            await message.answer("Доступ к боту закрыт администратором.")
            return
        await message.answer(
            "Доступ к боту предоставляется по приглашению. "
            "Вы можете отправить запрос администратору.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Запросить инвайт", callback_data=INVITE_CALLBACK_DATA)]
                ]
            ),
        )

    @router.message(CommandStart())
    async def start(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        if not await owners.is_authorized(message.from_user.id):
            await answer_access_denied(message, message.from_user.id)
            return

        owner, first_start = await owners.start_user(message.from_user.id, message.chat.id)
        if owner is None:
            await answer_access_denied(message, message.from_user.id)
            return
        if first_start:
            user = message.from_user
            username = f"@{user.username}" if user.username else "не указан"
            try:
                await bot.send_message(
                    admin_telegram_id,
                    "Новый пользователь начал пользоваться ботом:\n"
                    f"Пользователь: {user.full_name.replace(chr(10), ' ')[:128]}\n"
                    f"Username: {username}\n"
                    f"Telegram ID: {user.id}",
                )
            except TelegramAPIError:
                logger.exception("new_user_notification_failed")
        is_admin = message.from_user.id == admin_telegram_id
        admin_commands = (
            "\n/available_groups — показать группы подключённого аккаунта\n"
            "/add_user <Telegram ID> — восстановить доступ\n"
            "/remove_user <Telegram ID> — закрыть доступ\n"
            "/users или /list_users — список пользователей\n"
            "/report_today — отчёт с 00:00 по GMT+3\n"
            "/access_mode — режим входа пользователей\n"
            "/pause — поставить бота на паузу\n"
            "/resume — возобновить работу\n"
            "/pause_status — проверить паузу"
            if is_admin
            else ""
        )
        await message.answer(
            "Бот готов следить за группами, доступными подключённому аккаунту.\n\n"
            "Доступные команды:\n"
            "/help — справка\n"
            "/check_group <ссылка, @username или ID> — проверить доступ к группе\n"
            "/add — добавить группу для мониторинга\n"
            "/groups — управлять добавленными группами\n"
            "/feedback — предложить идею или оставить отзыв"
            f"{admin_commands}"
        )

    @router.callback_query(F.data == INVITE_CALLBACK_DATA)
    async def request_invite(callback: CallbackQuery, bot: Bot) -> None:
        if not isinstance(callback.message, Message) or callback.message.chat.type != "private":
            await callback.answer()
            return
        user = callback.from_user
        if await owners.is_authorized(user.id):
            await callback.answer("У вас уже есть доступ. Отправьте /start.", show_alert=True)
            return
        if await owners.is_disabled(user.id):
            await callback.answer("Доступ закрыт администратором.", show_alert=True)
            return

        async with invite_request_lock:
            now = datetime.now(UTC)
            previous = last_invite_request.get(user.id)
            if previous is not None and now - previous < INVITE_COOLDOWN:
                await callback.answer("Запрос уже отправлен. Подождите ответа администратора.", show_alert=True)
                return
            username = f"@{user.username}" if user.username else "не указан"
            try:
                await bot.send_message(
                    admin_telegram_id,
                    "Запрос доступа к боту:\n"
                    f"Пользователь: {user.full_name.replace(chr(10), ' ')[:128]}\n"
                    f"Username: {username}\n"
                    f"Telegram ID: {user.id}\n\n"
                    f"Чтобы дать доступ: /add_user {user.id}",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="Разрешить доступ",
                                    callback_data=f"{APPROVE_CALLBACK_PREFIX}{user.id}",
                                )
                            ]
                        ]
                    ),
                )
            except TelegramAPIError:
                logger.exception("invite_request_delivery_failed")
                await callback.answer("Не удалось отправить запрос. Попробуйте позже.", show_alert=True)
                return
            last_invite_request[user.id] = now
        await callback.answer("Запрос отправлен администратору.")
        try:
            await callback.message.edit_text("Запрос отправлен администратору. Ожидайте приглашения.")
        except TelegramBadRequest:
            logger.warning("invite_request_message_edit_failed")

    @router.my_chat_member()
    async def bot_block_state(event: ChatMemberUpdated) -> None:
        if event.chat.type != "private":
            return
        old_status = event.old_chat_member.status
        new_status = event.new_chat_member.status
        if new_status == ChatMemberStatus.KICKED and old_status != ChatMemberStatus.KICKED:
            await owners.record_bot_block_state(
                event.chat.id, blocked=True, occurred_at=event.date
            )
        elif old_status == ChatMemberStatus.KICKED and new_status != ChatMemberStatus.KICKED:
            await owners.record_bot_block_state(
                event.chat.id, blocked=False, occurred_at=event.date
            )

    return router


def create_router(
    *,
    admin_telegram_id: int,
    owners: OwnerRepository,
    resolve_group: GroupResolver,
    list_groups: GroupLister,
) -> Router:
    router = Router(name="owner_commands")
    router.message.middleware(AuthorizedUserMiddleware(owners))

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Ботом можно пользоваться сразу после /start.\n\n"
            "При /add подключённый аккаунт вступит в группу, если ещё не состоит в ней. "
            "Используйте публичную или пригласительную ссылку либо @username. "
            "По одному числовому ID вступить в группу нельзя. "
            "Ссылка на сообщение из темы позволяет отслеживать только эту тему. "
            "Если для вступления нужно одобрение, повторите /add после него.\n\n"
            "/add — добавить группу, /groups — изменить условия поиска, "
            "/feedback — отправить идею или отзыв, /cancel — отменить текущий диалог."
        )

    @router.message(Command("available_groups"))
    async def available_groups_command(message: Message) -> None:
        if message.from_user is None or message.from_user.id != admin_telegram_id:
            await message.answer("Полный список доступных аккаунту групп виден только админу.")
            return
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


def make_telegram_group_joiner(client) -> GroupResolver:
    async def joiner(reference: str) -> ResolvedGroup:
        return await resolve_or_join_group(client, reference)

    return joiner


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
