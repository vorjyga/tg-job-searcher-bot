import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User
from telethon import errors

from tg_jobs_searcher.bot.catalog import create_catalog_router, save_catalog_keywords
from tg_jobs_searcher.db.models import MatchSource
from tg_jobs_searcher.services.catalog import CatalogImportWorker, CatalogRecoveryWorker
from tg_jobs_searcher.services.live_messages import LiveMessageProcessor
from tg_jobs_searcher.telegram.client import GroupResolutionError, ResolvedGroup


def message(text: str, user_id: int = 42, chat_type: str = "private") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        text=text,
        chat=Chat(id=user_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="User"),
    )


@pytest.mark.asyncio
async def test_keyword_validation_keeps_dialog_and_does_not_replace_search():
    catalog = SimpleNamespace(set_search=AsyncMock())
    conversations = SimpleNamespace(clear=AsyncMock())
    with patch.object(Message, "answer", new_callable=AsyncMock):
        await save_catalog_keywords(
            message("python &"), uuid.uuid4(), catalog, conversations, "python &"
        )
    catalog.set_search.assert_not_awaited()
    conversations.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_maximum_keyword_list_has_bounded_confirmation():
    catalog = SimpleNamespace(set_search=AsyncMock(), source_count=AsyncMock(return_value=400))
    conversations = SimpleNamespace(clear=AsyncMock())
    text = ",".join(str(i) + "x" * 495 for i in range(50))
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await save_catalog_keywords(message(text), uuid.uuid4(), catalog, conversations, text)
    assert len(answer.await_args.args[0]) < 4096
    assert len(catalog.set_search.await_args.args[1]) == 50
    conversations.clear.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "chat_type", "allowed"),
    [(42, "private", False), (1, "private", True), (1, "group", False)],
)
async def test_source_import_is_admin_only_in_private_chat(user_id, chat_type, allowed):
    catalog = SimpleNamespace(enqueue_imports=AsyncMock(return_value=2))
    owners = SimpleNamespace(is_authorized=AsyncMock(return_value=True))
    with patch("tg_jobs_searcher.bot.catalog.CatalogRepository", return_value=catalog):
        router = create_catalog_router(admin_telegram_id=1, owners=owners, session_factory=None)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    try:
        with patch.object(Message, "answer", new_callable=AsyncMock):
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=message(
                        "/sources_add @first\nhttps://t.me/second",
                        user_id,
                        chat_type,
                    ),
                ),
            )
    finally:
        await bot.session.close()
    assert catalog.enqueue_imports.await_count == int(allowed)
    if allowed:
        catalog.enqueue_imports.assert_awaited_once_with(["@first", "https://t.me/second"])


@pytest.mark.asyncio
async def test_search_without_arguments_starts_persisted_dialog():
    owner_id = uuid.uuid4()
    owners = SimpleNamespace(ensure_owner=AsyncMock(return_value=SimpleNamespace(id=owner_id)))
    conversations = SimpleNamespace(set=AsyncMock())
    with patch("tg_jobs_searcher.bot.catalog.ConversationRepository", return_value=conversations):
        router = create_catalog_router(admin_telegram_id=1, owners=owners, session_factory=None)
    with patch.object(Message, "answer", new_callable=AsyncMock):
        await router.message.handlers[0].callback(message("/search"), SimpleNamespace(args=None))
    assert conversations.set.await_args.args[1].value == "awaiting_catalog_keywords"


@pytest.mark.asyncio
async def test_live_catalog_match_does_not_require_personal_groups():
    local = SimpleNamespace(active_groups_for_chat=AsyncMock(return_value=[]))
    catalog = SimpleNamespace(process_message=AsyncMock(return_value=2))
    processor = LiveMessageProcessor(local, catalog)
    assert (
        await processor.process(
            telegram_chat_id=-100123,
            telegram_message_id=10,
            message_date=datetime.now(UTC),
            text="Python",
        )
        == 2
    )
    catalog.process_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_import_preserves_wrapped_flood_wait_for_durable_retry():
    catalog = SimpleNamespace(finish_import=AsyncMock(), add_source=AsyncMock())
    try:
        raise errors.FloodWaitError(request=None, capture=120)
    except errors.FloodWaitError as exc:
        wrapped = GroupResolutionError("Telegram ограничил вступления")
        wrapped.__cause__ = exc
    worker = CatalogImportWorker(catalog, AsyncMock(side_effect=wrapped), None)
    job = SimpleNamespace(id=uuid.uuid4(), reference="@jobs", attempt_count=1)
    await worker._import(job)
    catalog.add_source.assert_not_awaited()
    assert catalog.finish_import.await_args.kwargs["retry_after"] == 120


@pytest.mark.asyncio
async def test_import_only_registers_successfully_resolved_source():
    catalog = SimpleNamespace(finish_import=AsyncMock(), add_source=AsyncMock())
    group = ResolvedGroup(-100123, "Jobs", "jobs")
    worker = CatalogImportWorker(catalog, AsyncMock(return_value=group), None)
    job = SimpleNamespace(id=uuid.uuid4(), reference="@jobs", attempt_count=1)
    await worker._import(job)
    catalog.add_source.assert_awaited_once_with(group)
    catalog.finish_import.assert_awaited_once_with(job.id)


class RecoveryClient:
    def __init__(self, posts):
        self.posts = posts
        self.options = []

    async def get_input_entity(self, chat_id):
        return chat_id

    async def iter_messages(self, entity, **options):
        self.options.append(options)
        if not options.get("reverse"):
            yield SimpleNamespace(id=100)
        else:
            for post in self.posts:
                yield post


def source():
    return SimpleNamespace(
        id=uuid.uuid4(),
        telegram_chat_id=-100123,
        monitoring_started_at=datetime.now(UTC) - timedelta(days=30),
        last_contiguous_message_id=10,
    )


@pytest.mark.asyncio
async def test_recovery_moves_past_old_history_and_checkpoints_after_processing():
    catalog = SimpleNamespace(process_message=AsyncMock(), recovery_progress=AsyncMock())
    client = RecoveryClient([SimpleNamespace(id=101, date=datetime.now(UTC), raw_text="Python")])
    worker = CatalogRecoveryWorker(catalog, client)
    item = source()
    assert await worker._recover(item) is None
    assert client.options[1]["min_id"] == 100
    assert client.options[1]["reverse"]
    assert catalog.process_message.await_args.kwargs["source"] == MatchSource.RECOVERY
    assert catalog.recovery_progress.await_args.args[2] == 101


@pytest.mark.asyncio
async def test_recovery_failure_leaves_cursor_for_retry():
    catalog = SimpleNamespace(
        process_message=AsyncMock(side_effect=RuntimeError("database unavailable")),
        recovery_progress=AsyncMock(),
    )
    client = RecoveryClient([SimpleNamespace(id=101, date=datetime.now(UTC), raw_text="Python")])
    worker = CatalogRecoveryWorker(catalog, client)
    await worker._recover(source())
    assert catalog.recovery_progress.await_args.args[2] is None
    assert catalog.recovery_progress.await_args.kwargs["delay"] == 60


@pytest.mark.asyncio
async def test_recovery_access_loss_disables_catalog_source():
    catalog = SimpleNamespace(recovery_progress=AsyncMock())
    client = SimpleNamespace(
        get_input_entity=AsyncMock(side_effect=errors.ChannelPrivateError(request=None))
    )
    worker = CatalogRecoveryWorker(catalog, client)
    await worker._recover(source())
    assert catalog.recovery_progress.await_args.kwargs["access_lost"]


@pytest.mark.asyncio
async def test_catalog_keyword_dialog_is_handled_by_existing_management_router():
    from tg_jobs_searcher.bot.management import create_management_router
    from tg_jobs_searcher.db.repository_types import ConversationStep

    owner_id = uuid.uuid4()
    owners = SimpleNamespace(ensure_owner=AsyncMock(return_value=SimpleNamespace(id=owner_id)))
    conversations = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(step=ConversationStep.AWAITING_CATALOG_KEYWORDS.value)
        ),
        clear=AsyncMock(),
    )
    catalog = SimpleNamespace(set_search=AsyncMock(), source_count=AsyncMock(return_value=400))
    with (
        patch("tg_jobs_searcher.bot.management.ConversationRepository", return_value=conversations),
        patch("tg_jobs_searcher.bot.management.CatalogRepository", return_value=catalog),
    ):
        router = create_management_router(
            admin_telegram_id=1,
            owners=owners,
            session_factory=None,
            resolve_group=AsyncMock(),
            check_group_access=AsyncMock(),
        )
        with patch.object(Message, "answer", new_callable=AsyncMock):
            await router.message.handlers[-1].callback(message("python & вакансия, react"), None)
    catalog.set_search.assert_awaited_once()
    assert catalog.set_search.await_args.args[0] == owner_id
    assert [rule.terms for rule in catalog.set_search.await_args.args[1]] == [
        ("python", "вакансия"),
        ("react",),
    ]
    conversations.clear.assert_awaited_once_with(owner_id)
