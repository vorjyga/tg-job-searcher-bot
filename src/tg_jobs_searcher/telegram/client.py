"""Telethon client lifecycle and safe group resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from telethon import TelegramClient, errors, utils
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    GetForumTopicsByIDRequest,
    ImportChatInviteRequest,
)

from tg_jobs_searcher.config import TelethonSettings
from tg_jobs_searcher.telegram.auth import read_string_session


class TelegramAuthorizationError(RuntimeError):
    """Raised when the stored account session is no longer authorised."""


class GroupResolutionError(ValueError):
    """Raised when a requested group or channel is inaccessible or unsupported."""


@dataclass(frozen=True, slots=True)
class ResolvedGroup:
    telegram_chat_id: int
    title: str
    username: str | None
    topic_id: int | None = None
    topic_title: str | None = None


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
    """Normalise public Telegram chat URLs, usernames and numeric chat IDs."""
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
                "Use a public group link, @username or numeric chat ID here; "
                "invite links can be used with /add"
            )
        return f"@{username}"

    raise GroupResolutionError("Use a public group link, @username or numeric chat ID")


async def resolve_accessible_group(client: TelegramClient, reference: str) -> ResolvedGroup:
    """Resolve a group or channel present in the connected account's dialogs."""
    reference, topic_message_id = _split_topic_reference(reference)
    parsed_reference = parse_group_reference(reference)
    try:
        entity = await client.get_entity(parsed_reference)
    except (errors.RPCError, ValueError) as exc:
        raise GroupResolutionError("Не удалось найти группу или канал") from exc

    if not _is_supported_chat(entity):
        raise GroupResolutionError("Это не Telegram-группа или канал")

    chat_id = utils.get_peer_id(entity)
    if not await _is_accessible_dialog(client, chat_id):
        raise GroupResolutionError(
            "Подключённый Telegram-аккаунт не состоит в этой группе или канале либо не может читать сообщения"
        )
    topic_id = None
    topic_title = None
    if topic_message_id is not None:
        if not isinstance(entity, types.Channel) or not entity.forum:
            raise GroupResolutionError("This group does not have topics")
        try:
            message = await client.get_messages(entity, ids=topic_message_id)
            if message is None or isinstance(message, types.MessageEmpty):
                raise GroupResolutionError("The linked message is not accessible")
            reply = getattr(message, "reply_to", None)
            candidates = [
                getattr(reply, "reply_to_top_id", None),
                getattr(reply, "reply_to_msg_id", None),
                message.id,
            ]
            candidates = [value for value in dict.fromkeys(candidates) if value is not None]
            topics = await client(GetForumTopicsByIDRequest(peer=entity, topics=candidates))
            topic = next(
                (item for candidate in candidates for item in topics.topics if item.id == candidate),
                None,
            )
            if topic is None:
                raise GroupResolutionError("The linked message is not in an accessible topic")
            topic_id, topic_title = topic.id, topic.title
        except errors.RPCError as exc:
            raise GroupResolutionError("Could not resolve the topic from this link") from exc
    return ResolvedGroup(
        telegram_chat_id=chat_id,
        title=entity.title,
        username=getattr(entity, "username", None),
        topic_id=topic_id,
        topic_title=topic_title,
    )


async def resolve_or_join_group(client: TelegramClient, reference: str) -> ResolvedGroup:
    """Join a submitted group or channel when possible, then resolve it for /add."""
    invite_hash = _invite_hash(reference)
    if invite_hash is not None:
        return await _resolve_or_join_invite(client, invite_hash)

    base_reference, _ = _split_topic_reference(reference)
    parsed_reference = parse_group_reference(base_reference)
    if isinstance(parsed_reference, int):
        try:
            return await resolve_accessible_group(client, reference)
        except GroupResolutionError as exc:
            raise GroupResolutionError(
                "По одному числовому ID нельзя вступить в группу или канал. "
                "Отправьте публичную или пригласительную ссылку."
            ) from exc

    try:
        entity = await client.get_entity(parsed_reference)
    except (errors.RPCError, ValueError) as exc:
        raise GroupResolutionError("Не удалось найти группу или канал по этой ссылке или @username") from exc
    if not _is_supported_chat(entity):
        raise GroupResolutionError("Это не Telegram-группа или канал")

    if not await _is_accessible_dialog(client, utils.get_peer_id(entity)):
        if not isinstance(entity, types.Channel):
            raise GroupResolutionError("Для вступления в эту группу нужна пригласительная ссылка")
        try:
            await client(JoinChannelRequest(entity))
        except errors.UserAlreadyParticipantError:
            pass
        except errors.InviteRequestSentError as exc:
            raise GroupResolutionError(
                "Заявка на вступление отправлена. После одобрения повторите /add."
            ) from exc
        except errors.FloodWaitError as exc:
            raise GroupResolutionError(
                f"Telegram ограничил вступления. Повторите через {exc.seconds} секунд."
            ) from exc
        except errors.RPCError as exc:
            raise GroupResolutionError("Telegram не разрешил вступить в эту группу или канал") from exc
    return await resolve_accessible_group(client, reference)


async def _resolve_or_join_invite(client: TelegramClient, invite_hash: str) -> ResolvedGroup:
    try:
        preview = await client(CheckChatInviteRequest(invite_hash))
    except errors.RPCError as exc:
        raise GroupResolutionError("Пригласительная ссылка недействительна или истекла") from exc

    if isinstance(preview, types.ChatInviteAlready):
        entity = preview.chat
    else:
        if isinstance(preview, types.ChatInvitePeek) and not _is_supported_chat(preview.chat):
            raise GroupResolutionError("Ссылка не ведёт на группу или канал")
        try:
            updates = await client(ImportChatInviteRequest(invite_hash))
        except errors.UserAlreadyParticipantError:
            # The account joined between the preview and the import; retry the preview.
            latest = await client(CheckChatInviteRequest(invite_hash))
            if not isinstance(latest, types.ChatInviteAlready):
                raise GroupResolutionError("Не удалось подтвердить вступление в группу или канал") from None
            entity = latest.chat
        except errors.InviteRequestSentError as exc:
            raise GroupResolutionError(
                "Заявка на вступление отправлена. После одобрения повторите /add."
            ) from exc
        except errors.FloodWaitError as exc:
            raise GroupResolutionError(
                f"Telegram ограничил вступления. Повторите через {exc.seconds} секунд."
            ) from exc
        except errors.RPCError as exc:
            raise GroupResolutionError("Telegram не разрешил вступить по этой ссылке") from exc
        else:
            entity = next((chat for chat in updates.chats if _is_supported_chat(chat)), None)
            if entity is None:
                raise GroupResolutionError("Не удалось подтвердить вступление в группу или канал")

    if not _is_supported_chat(entity):
        raise GroupResolutionError("Ссылка не ведёт на группу или канал")
    chat_id = utils.get_peer_id(entity)
    if not await _is_accessible_dialog(client, chat_id):
        raise GroupResolutionError("Вступление ещё не подтверждено. Повторите /add позже.")
    return ResolvedGroup(
        telegram_chat_id=chat_id,
        title=entity.title,
        username=getattr(entity, "username", None),
    )


def _split_topic_reference(reference: str) -> tuple[str, int | None]:
    parts = urlsplit(reference.strip() if "://" in reference else f"https://{reference.strip()}")
    if parts.netloc in {"t.me", "www.t.me"}:
        path = parts.path.strip("/").split("/")
        if len(path) == 2 and path[1].isdigit() and int(path[1]) > 0:
            return f"https://t.me/{path[0]}", int(path[1])
    return reference, None


def _invite_hash(reference: str) -> str | None:
    candidate = reference.strip()
    parts = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
    if parts.netloc not in {"t.me", "www.t.me"}:
        return None
    path = parts.path.strip("/")
    if path.startswith("+"):
        invite_hash = path[1:]
    elif path.startswith("joinchat/"):
        invite_hash = path.removeprefix("joinchat/")
    else:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", invite_hash):
        raise GroupResolutionError("Неверный формат пригласительной ссылки")
    return invite_hash


async def list_accessible_groups(client: TelegramClient) -> list[ResolvedGroup]:
    groups: list[ResolvedGroup] = []
    async for dialog in client.iter_dialogs():
        if not _is_supported_dialog(dialog):
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
        if dialog.id == chat_id and _is_supported_dialog(dialog):
            return True
    return False


def _is_supported_dialog(dialog: object) -> bool:
    return bool(getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False)) and (
        _is_supported_chat(dialog.entity)
    )


def _is_supported_chat(entity: object) -> bool:
    return isinstance(entity, types.Chat) or (
        isinstance(entity, types.Channel) and bool(entity.megagroup or entity.broadcast)
    )


def _is_integer(value: str) -> bool:
    return value.lstrip("-").isdigit()
