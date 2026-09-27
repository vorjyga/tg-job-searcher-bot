from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat, MenuButtonCommands

from tg_jobs_searcher.bot.command_menu import (
    ADMIN_COMMANDS,
    USER_COMMANDS,
    command_help,
    configure_command_menu,
)


@pytest.mark.asyncio
async def test_command_menu_has_user_and_admin_scopes() -> None:
    bot = SimpleNamespace(set_my_commands=AsyncMock(), set_chat_menu_button=AsyncMock())

    await configure_command_menu(bot, 1466409)  # type: ignore[arg-type]

    user_call, admin_call = bot.set_my_commands.await_args_list
    assert isinstance(user_call.kwargs["scope"], BotCommandScopeAllPrivateChats)
    assert isinstance(admin_call.kwargs["scope"], BotCommandScopeChat)
    assert admin_call.kwargs["scope"].chat_id == 1466409
    assert [item.command for item in user_call.args[0]] == [item.name for item in USER_COMMANDS]
    assert [item.command for item in admin_call.args[0]] == [
        item.name for item in (*USER_COMMANDS, *ADMIN_COMMANDS)
    ]
    assert bot.set_chat_menu_button.await_count == 2
    assert isinstance(bot.set_chat_menu_button.await_args.kwargs["menu_button"], MenuButtonCommands)


def test_help_uses_same_command_catalog_as_menu() -> None:
    user_help = command_help(is_admin=False)
    admin_help = command_help(is_admin=True)
    for command in USER_COMMANDS:
        assert command.help_line() in user_help
        assert command.help_line() in admin_help
    for command in ADMIN_COMMANDS:
        assert command.help_line() not in user_help
        assert command.help_line() in admin_help
