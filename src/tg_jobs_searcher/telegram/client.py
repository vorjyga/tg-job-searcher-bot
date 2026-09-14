"""Telethon client lifecycle and safe group resolution."""

from __future__ import annotations

from dataclasses import dataclass

from telethon import TelegramClient, errors, utils
from telethon.sessions import StringSession
from telethon.tl import types

from tg_jobs_searcher.config import TelethonSettings
from tg_jobs_searcher.telegram.auth import read_string_session


class TelegramAuthorizationError(RuntimeError):
    """Raised when the stored account session is no longer authorised."""


class GroupResolutionError(ValueError):
    """Raised when a requested chat is inaccessible or is not a Telegram group."""


@dataclass(frozen=True, slots=True)
class ResolvedGroup:
    telegram_chat_id: int
    title: str
    username: str | None


def create_telegram_client(settings: TelethonSettings) -> TelegramClient:
    return TelegramClient(
        StringSession(read_string_session(settings.session_path)),
        settings.api_id,
        settings.api_hash,
    )


async def connect_authorized_client(client: TelegramClient) -> None:
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise TelegramAuthorizationError(
            "Telethon session is not authorised; run `tg-jobs-searcher auth` again"
        )


def parse_group_reference(value: str) -> str | int:
    """Normalise public Telegram group URLs, usernames and numeric chat IDs."""
    candidate = value.strip()
    if not candidate:
        raise GroupResolutionError("Group reference is empty")
    if _is_integer(candidate):
        return int(candidate)
    if candidate.startswith("@"):
        if len(candidate) == 1:
            raise GroupResolutionError("Telegram username is empty")
        return candidate

    normalised = candidate.removeprefix("https://").removeprefix("http://").rstrip("/")
    if normalised.startswith("t.me/"):
        username = normalised.removeprefix("t.me/")
        if not username or username.startswith(("+", "joinchat/")) or "/" in username:
            raise GroupResolutionError(
                "Use a public group link, @username or numeric chat ID; invite links are not supported"
            )
        return f"@{username}"

    raise GroupResolutionError("Use a public group link, @username or numeric chat ID")


async def resolve_accessible_group(client: TelegramClient, reference: str) -> ResolvedGroup:
    """Resolve a group and confirm that the connected account has it in its dialogs."""
    parsed_reference = parse_group_reference(reference)
    try:
        entity = await client.get_entity(parsed_reference)
    except (errors.RPCError, ValueError) as exc:
        raise GroupResolutionError("Could not resolve this Telegram group") from exc

    if not _is_supported_group(entity):
        raise GroupResolutionError("This chat is not a Telegram group")

    chat_id = utils.get_peer_id(entity)
    if not await _is_accessible_dialog(client, chat_id):
        raise GroupResolutionError(
            "The connected Telegram account is not a member of this group or cannot read it"
        )
    return ResolvedGroup(
        telegram_chat_id=chat_id,
        title=entity.title,
        username=getattr(entity, "username", None),
    )


async def list_accessible_groups(client: TelegramClient) -> list[ResolvedGroup]:
    groups: list[ResolvedGroup] = []
    async for dialog in client.iter_dialogs():
        if not dialog.is_group or not _is_supported_group(dialog.entity):
            continue
        groups.append(
            ResolvedGroup(
                telegram_chat_id=dialog.id,
                title=dialog.name,
                username=getattr(dialog.entity, "username", None),
            )
        )
    return groups


async def _is_accessible_dialog(client: TelegramClient, chat_id: int) -> bool:
    async for dialog in client.iter_dialogs():
        if dialog.id == chat_id and dialog.is_group and _is_supported_group(dialog.entity):
            return True
    return False


def _is_supported_group(entity: object) -> bool:
    return isinstance(entity, types.Chat) or (
        isinstance(entity, types.Channel) and bool(entity.megagroup)
    )


def _is_integer(value: str) -> bool:
    return value.lstrip("-").isdigit()
