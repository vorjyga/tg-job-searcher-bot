"""Block bot interactions while the administrator has paused the service."""

from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from tg_jobs_searcher.services.pause import PauseCoordinator

PAUSED_MESSAGE = (
    "Бот на паузе. Настройки и доступ сейчас не меняются; уведомления придут после возобновления."
)
CONTROL_COMMANDS = frozenset({"/pause", "/resume", "/pause_status"})


class PauseMiddleware(BaseMiddleware):
    def __init__(self, coordinator: PauseCoordinator, admin_telegram_id: int) -> None:
        self._coordinator = coordinator
        self._admin_telegram_id = admin_telegram_id

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message) and self._is_admin_control_command(event):
            return await handler(event, data)
        async with self._coordinator.activity() as allowed:
            if allowed:
                return await handler(event, data)
        if isinstance(event, Message) and event.chat.type == "private":
            await event.answer(PAUSED_MESSAGE)
        elif isinstance(event, CallbackQuery):
            await event.answer(
                "Бот на паузе. Повторите действие после возобновления.", show_alert=True
            )
        return None

    def _is_admin_control_command(self, message: Message) -> bool:
        if (
            message.from_user is None
            or message.from_user.id != self._admin_telegram_id
            or message.chat.type != "private"
            or not message.text
        ):
            return False
        command = message.text.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
        return command in CONTROL_COMMANDS
