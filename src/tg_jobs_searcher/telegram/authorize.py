"""Interactive command for authorising the Telegram account once."""

from __future__ import annotations

import asyncio
import getpass
from collections.abc import Callable

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from tg_jobs_searcher.config import TelethonSettings
from tg_jobs_searcher.telegram.auth import write_string_session


async def authorise_account(
    settings: TelethonSettings,
    *,
    phone: str | None = None,
    input_func: Callable[[str], str] = input,
    password_func: Callable[[str], str] = getpass.getpass,
) -> None:
    """Complete Telegram's login flow and persist only the resulting StringSession."""
    phone_number = phone or input_func("Telegram phone number (for example +995...): ").strip()
    if not phone_number:
        raise ValueError("Phone number is required")

    client = TelegramClient(StringSession(), settings.api_id, settings.api_hash)
    try:
        await client.connect()
        await client.send_code_request(phone_number)
        code = input_func("Code from Telegram: ").strip()
        if not code:
            raise ValueError("Telegram confirmation code is required")
        try:
            await client.sign_in(phone=phone_number, code=code)
        except errors.SessionPasswordNeededError:
            password = password_func("Telegram two-step verification password: ")
            await client.sign_in(password=password)
        write_string_session(settings.session_path, client.session.save())
    finally:
        await client.disconnect()


def run_authorisation(settings: TelethonSettings, phone: str | None = None) -> int:
    try:
        asyncio.run(authorise_account(settings, phone=phone))
    except (errors.RPCError, OSError, ValueError) as exc:
        print(f"Telegram authorisation failed: {exc}")
        return 1
    print(f"Telethon session saved to {settings.session_path}")
    return 0
