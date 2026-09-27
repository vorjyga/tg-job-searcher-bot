"""Command catalog shared by Telegram's menu and the in-chat help."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    MenuButtonCommands,
)


@dataclass(frozen=True, slots=True)
class CommandInfo:
    name: str
    description: str
    usage: str | None = None

    def as_bot_command(self) -> BotCommand:
        return BotCommand(command=self.name, description=self.description)

    def help_line(self) -> str:
        return f"/{self.usage or self.name} — {self.description}"


USER_COMMANDS = (
    CommandInfo("start", "начать работу"),
    CommandInfo("help", "все команды и справка"),
    CommandInfo("add", "добавить группу для мониторинга"),
    CommandInfo("groups", "управлять группами и условиями поиска"),
    CommandInfo("status", "статус сканирования"),
    CommandInfo(
        "check_group", "проверить доступ к группе", "check_group <ссылка, @username или ID>"
    ),
    CommandInfo("feedback", "отправить идею или отзыв"),
    CommandInfo("cancel", "отменить текущий диалог"),
)

ADMIN_COMMANDS = (
    CommandInfo("available_groups", "группы подключённого аккаунта"),
    CommandInfo("add_user", "открыть доступ пользователю", "add_user <Telegram ID>"),
    CommandInfo("remove_user", "закрыть доступ пользователю", "remove_user <Telegram ID>"),
    CommandInfo("users", "список пользователей"),
    CommandInfo("list_users", "список пользователей (альтернативная команда)"),
    CommandInfo("report_today", "отчёт за сегодня"),
    CommandInfo("access_mode", "режим входа пользователей"),
    CommandInfo("pause", "поставить бота на паузу"),
    CommandInfo("resume", "возобновить работу бота"),
    CommandInfo("pause_status", "проверить состояние паузы"),
)


def command_help(*, is_admin: bool) -> str:
    user_lines = "\n".join(command.help_line() for command in USER_COMMANDS)
    if not is_admin:
        return user_lines
    admin_lines = "\n".join(command.help_line() for command in ADMIN_COMMANDS)
    return f"{user_lines}\n\nКоманды администратора:\n{admin_lines}"


async def configure_command_menu(bot: Bot, admin_telegram_id: int) -> None:
    """Show the user's commands in private chats and the full list to the admin."""
    await bot.set_my_commands(
        [command.as_bot_command() for command in USER_COMMANDS],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [command.as_bot_command() for command in (*USER_COMMANDS, *ADMIN_COMMANDS)],
        scope=BotCommandScopeChat(chat_id=admin_telegram_id),
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await bot.set_chat_menu_button(
        chat_id=admin_telegram_id,
        menu_button=MenuButtonCommands(),
    )
