from types import SimpleNamespace

import pytest
from telethon import utils
from telethon.tl import types
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

from tg_jobs_searcher.telegram.client import (
    GroupResolutionError,
    list_accessible_groups,
    parse_group_reference,
    resolve_accessible_group,
    resolve_or_join_group,
)


class FakeDialog:
    def __init__(self, entity, *, is_group: bool = True, is_channel: bool = False) -> None:
        self.entity = entity
        self.id = utils.get_peer_id(entity)
        self.is_group = is_group
        self.is_channel = is_channel
        self.name = entity.title


class FakeClient:
    def __init__(self, entity, dialogs: list[FakeDialog]) -> None:
        self.entity = entity
        self.dialogs = dialogs

    async def get_entity(self, reference):
        return self.entity

    async def iter_dialogs(self):
        for dialog in self.dialogs:
            yield dialog


class FakeTopicClient(FakeClient):
    async def get_messages(self, entity, *, ids):
        return SimpleNamespace(id=ids, reply_to=SimpleNamespace(reply_to_top_id=46600))

    async def __call__(self, request):
        return SimpleNamespace(topics=[SimpleNamespace(id=46600, title="Вакансии")])


class FakeJoinClient(FakeClient):
    def __init__(self, entity, dialogs: list[FakeDialog], invite_preview=None) -> None:
        super().__init__(entity, dialogs)
        self.invite_preview = invite_preview
        self.join_requests = []

    async def __call__(self, request):
        self.join_requests.append(request)
        if isinstance(request, CheckChatInviteRequest):
            return self.invite_preview
        if isinstance(request, (JoinChannelRequest, ImportChatInviteRequest)):
            self.dialogs.append(
                FakeDialog(
                    self.entity,
                    is_group=not bool(getattr(self.entity, "broadcast", False)),
                    is_channel=bool(getattr(self.entity, "broadcast", False)),
                )
            )
            return SimpleNamespace(chats=[self.entity])
        raise AssertionError(f"Unexpected request: {type(request).__name__}")


def make_chat() -> types.Chat:
    return types.Chat(
        id=123,
        title="Python Jobs",
        photo=None,
        participants_count=2,
        date=None,
        version=1,
    )


def make_megagroup() -> types.Channel:
    return types.Channel(
        id=456,
        title="Remote jobs",
        photo=None,
        date=None,
        megagroup=True,
        username="remote_jobs",
    )


def make_channel() -> types.Channel:
    return types.Channel(
        id=777,
        title="Freelanly",
        photo=None,
        date=None,
        broadcast=True,
        megagroup=False,
        username="PCFTI",
    )


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("@python_jobs", "@python_jobs"),
        ("https://t.me/python_jobs", "@python_jobs"),
        ("t.me/python_jobs/", "@python_jobs"),
        ("-1001234567890", -1001234567890),
    ],
)
def test_parses_supported_group_reference(reference: str, expected: str | int) -> None:
    assert parse_group_reference(reference) == expected


@pytest.mark.parametrize(
    "reference", ["", "@", "https://t.me/+secret", "https://t.me/joinchat/abc"]
)
def test_rejects_unsupported_group_reference(reference: str) -> None:
    with pytest.raises(GroupResolutionError):
        parse_group_reference(reference)


@pytest.mark.asyncio
async def test_resolves_only_group_present_in_account_dialogs() -> None:
    chat = make_chat()
    client = FakeClient(chat, [FakeDialog(chat)])

    result = await resolve_accessible_group(client, "@python_jobs")

    assert result.title == "Python Jobs"
    assert result.telegram_chat_id == -123


@pytest.mark.asyncio
async def test_resolves_topic_from_public_message_link() -> None:
    channel = types.Channel(
        id=123, title="IT jobs", photo=None, date=None,
        megagroup=True, forum=True,
    )
    client = FakeTopicClient(channel, [FakeDialog(channel)])

    result = await resolve_accessible_group(client, "https://t.me/cyprusithr/46685")

    assert result.topic_id == 46600
    assert result.topic_title == "Вакансии"


@pytest.mark.asyncio
async def test_rejects_group_not_in_account_dialogs() -> None:
    chat = make_chat()
    client = FakeClient(chat, [])

    with pytest.raises(GroupResolutionError, match="не состоит"):
        await resolve_accessible_group(client, "@python_jobs")


@pytest.mark.asyncio
async def test_add_joins_public_group_when_account_is_not_member() -> None:
    group = make_megagroup()
    client = FakeJoinClient(group, [])

    resolved = await resolve_or_join_group(client, "https://t.me/remote_jobs")

    assert resolved.telegram_chat_id == utils.get_peer_id(group)
    assert len(client.join_requests) == 1
    assert isinstance(client.join_requests[0], JoinChannelRequest)


@pytest.mark.asyncio
async def test_add_does_not_join_group_twice() -> None:
    group = make_megagroup()
    client = FakeJoinClient(group, [FakeDialog(group)])

    await resolve_or_join_group(client, "@remote_jobs")

    assert client.join_requests == []


@pytest.mark.asyncio
async def test_add_joins_private_group_with_invite_link() -> None:
    group = make_megagroup()
    preview = types.ChatInvite(
        title=group.title,
        photo=types.PhotoEmpty(id=0),
        participants_count=2,
        color=0,
        channel=True,
        megagroup=True,
    )
    client = FakeJoinClient(group, [], preview)

    resolved = await resolve_or_join_group(client, "https://t.me/+SecretInvite")

    assert resolved.telegram_chat_id == utils.get_peer_id(group)
    assert [type(request) for request in client.join_requests] == [
        CheckChatInviteRequest,
        ImportChatInviteRequest,
    ]


@pytest.mark.asyncio
async def test_add_joins_broadcast_channel_with_invite_link() -> None:
    channel = make_channel()
    preview = types.ChatInvite(
        title=channel.title,
        photo=types.PhotoEmpty(id=0),
        participants_count=10,
        color=0,
        channel=True,
        broadcast=True,
    )
    client = FakeJoinClient(channel, [], preview)

    resolved = await resolve_or_join_group(client, "t.me/joinchat/InviteHash")

    assert resolved.telegram_chat_id == utils.get_peer_id(channel)
    assert [type(request) for request in client.join_requests] == [
        CheckChatInviteRequest,
        ImportChatInviteRequest,
    ]


@pytest.mark.asyncio
async def test_add_subscribes_to_public_broadcast_channel() -> None:
    channel = make_channel()
    client = FakeJoinClient(channel, [])

    resolved = await resolve_or_join_group(client, "https://t.me/PCFTI")

    assert resolved.title == "Freelanly"
    assert resolved.telegram_chat_id == utils.get_peer_id(channel)
    assert [type(request) for request in client.join_requests] == [JoinChannelRequest]


@pytest.mark.asyncio
async def test_resolves_channel_already_in_dialogs() -> None:
    channel = make_channel()
    client = FakeClient(channel, [FakeDialog(channel, is_group=False, is_channel=True)])

    resolved = await resolve_accessible_group(client, "@PCFTI")

    assert resolved.telegram_chat_id == utils.get_peer_id(channel)


@pytest.mark.asyncio
async def test_add_rejects_entity_that_is_neither_group_nor_channel() -> None:
    client = FakeClient(object(), [])

    with pytest.raises(GroupResolutionError, match="Это не Telegram-группа или канал"):
        await resolve_or_join_group(client, "@some_user")


@pytest.mark.asyncio
async def test_numeric_id_cannot_join_unknown_group() -> None:
    group = make_megagroup()
    client = FakeJoinClient(group, [])

    with pytest.raises(GroupResolutionError, match="числовому ID"):
        await resolve_or_join_group(client, str(utils.get_peer_id(group)))

    assert client.join_requests == []


@pytest.mark.asyncio
async def test_includes_broadcast_channels_in_available_chats() -> None:
    chat = make_chat()
    channel = make_channel()
    client = FakeClient(chat, [FakeDialog(chat), FakeDialog(channel, is_group=False, is_channel=True)])

    groups = await list_accessible_groups(client)

    assert [group.title for group in groups] == ["Python Jobs", "Freelanly"]
