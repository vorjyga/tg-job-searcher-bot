import pytest
from telethon import utils
from telethon.tl import types

from tg_jobs_searcher.telegram.client import (
    GroupResolutionError,
    list_accessible_groups,
    parse_group_reference,
    resolve_accessible_group,
)


class FakeDialog:
    def __init__(self, entity, *, is_group: bool = True) -> None:
        self.entity = entity
        self.id = utils.get_peer_id(entity)
        self.is_group = is_group
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


def make_chat() -> types.Chat:
    return types.Chat(
        id=123,
        title="Python Jobs",
        photo=None,
        participants_count=2,
        date=None,
        version=1,
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
async def test_rejects_group_not_in_account_dialogs() -> None:
    chat = make_chat()
    client = FakeClient(chat, [])

    with pytest.raises(GroupResolutionError, match="not a member"):
        await resolve_accessible_group(client, "@python_jobs")


@pytest.mark.asyncio
async def test_excludes_broadcast_channels_from_available_groups() -> None:
    chat = make_chat()
    channel = types.Channel(
        id=456,
        title="News channel",
        photo=None,
        date=None,
        broadcast=True,
        megagroup=False,
    )
    client = FakeClient(chat, [FakeDialog(chat), FakeDialog(channel, is_group=False)])

    groups = await list_accessible_groups(client)

    assert [group.title for group in groups] == ["Python Jobs"]
