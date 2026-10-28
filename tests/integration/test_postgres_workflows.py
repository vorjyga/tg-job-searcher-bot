"""Run repository workflows against migrations in an isolated PostgreSQL schema."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tg_jobs_searcher.db.control_repository import BotControlRepository
from tg_jobs_searcher.db.models import (
    GroupStatus,
    Keyword,
    MatchSource,
    MessageMatch,
    NotificationOutbox,
    Owner,
    ScanJob,
    ScanJobType,
    TrackedGroup,
    WorkStatus,
)
from tg_jobs_searcher.db.repositories import (
    GroupRepository,
    MonitoringRepository,
    NotificationRepository,
    ScanMatch,
    ScanRepository,
)
from tg_jobs_searcher.services.keywords import KeywordInputError, parse_keyword_input
from tg_jobs_searcher.telegram.client import ResolvedGroup

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def sessions():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_DATABASE_URL must use postgresql+asyncpg")

    schema = f"tg_jobs_test_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(url, poolclass=NullPool)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:

        def upgrade(connection):
            config = Config(str(PROJECT_ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        async with engine.begin() as connection:
            await connection.run_sync(upgrade)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


async def _create_group(sessions, *, status: GroupStatus = GroupStatus.ACTIVE):
    owner = Owner(telegram_user_id=42, notification_chat_id=42, is_enabled=True)
    group = TrackedGroup(
        owner=owner,
        telegram_chat_id=-100123,
        title="Python jobs",
        status=status,
        monitoring_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    async with sessions() as session, session.begin():
        session.add_all([owner, group])
        await session.flush()
        session.add(
            Keyword(owner_id=owner.id, value="Python", normalized_value="python", terms=["python"])
        )
    return owner.id, group.id


@pytest.mark.asyncio
async def test_pause_state_survives_repository_recreation(sessions) -> None:
    control = BotControlRepository(sessions)
    assert not await control.is_paused()
    assert await control.set_paused(True)
    assert await BotControlRepository(sessions).is_paused()
    assert not await control.set_paused(True)
    assert await control.set_paused(False)
    assert not await BotControlRepository(sessions).is_paused()


@pytest.mark.asyncio
async def test_migrated_schema_persists_one_live_match_and_delivery(sessions) -> None:
    _, group_id = await _create_group(sessions)
    monitoring = MonitoringRepository(sessions)
    notifications = NotificationRepository(sessions)
    now = datetime.now(UTC)

    async with sessions() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0010_shared_keywords"
        )

    groups = await monitoring.active_groups_for_chat(-100123, now)
    assert len(groups) == 1
    assert [keyword.value for keyword in groups[0].keywords] == ["Python"]

    arguments = dict(
        group_id=group_id,
        telegram_message_id=77,
        message_date=now,
        matched_keywords=["Python"],
        payload="Python vacancy",
    )
    assert await monitoring.record_live_match(**arguments)
    assert not await monitoring.record_live_match(**arguments)

    async with sessions() as locked_session, locked_session.begin():
        await locked_session.scalar(select(NotificationOutbox).with_for_update())
        assert await notifications.claim_batch() == []

    deliveries = await notifications.claim_batch()
    assert len(deliveries) == 1
    assert await notifications.is_deliverable(deliveries[0])
    await notifications.mark_sent(deliveries[0].id, 1234)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MessageMatch)) == 1
        outbox = await session.scalar(select(NotificationOutbox))
        assert outbox is not None
        assert outbox.status == WorkStatus.COMPLETED
        assert outbox.telegram_notification_message_id == 1234


@pytest.mark.asyncio
async def test_restored_group_scans_gap_and_enforces_keyword_limit(sessions) -> None:
    owner_id, group_id = await _create_group(sessions, status=GroupStatus.ACCESS_LOST)
    groups = GroupRepository(sessions)
    scans = ScanRepository(sessions)

    await groups.restore_access(owner_id, group_id, ResolvedGroup(-100123, "Python jobs", None))
    lease = await scans.claim_next_job()
    assert lease is not None
    assert lease.job_type == ScanJobType.RECOVERY
    assert lease.keyword_snapshot == [{"value": "Python", "terms": ["python"]}]

    progress = await scans.persist_page(
        job_id=lease.id,
        cursor_message_id=78,
        high_watermark_message_id=78,
        messages_checked=1,
        matches=[
            ScanMatch(
                telegram_message_id=78,
                message_date=lease.range_end - timedelta(seconds=1),
                matched_keywords=["Python"],
                payload="Python vacancy",
            )
        ],
    )
    assert progress.matches_inserted == 1
    await scans.complete_job(lease.id)

    async with sessions() as session, session.begin():
        group = await session.get(TrackedGroup, group_id)
        match = await session.scalar(select(MessageMatch))
        assert group is not None and group.last_contiguous_message_id == 78
        assert match is not None and match.source == MatchSource.RECOVERY
        for index in range(49):
            session.add(
                Keyword(
                    owner_id=owner_id,
                    value=f"keyword{index}",
                    normalized_value=f"keyword{index}",
                    terms=[f"keyword{index}"],
                )
            )

    with pytest.raises(KeywordInputError, match="не более 50"):
        await groups.append_keywords(owner_id, group_id, parse_keyword_input("extra"))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Keyword)) == 50


@pytest.mark.asyncio
async def test_shared_keywords_apply_to_existing_new_and_reactivated_groups(sessions):
    owner_id, first_id = await _create_group(sessions)
    groups = GroupRepository(sessions)
    second = await groups.create_or_reactivate(
        owner_id, ResolvedGroup(-100456, "Other jobs", None), scan_history=True
    )
    assert [k.value for k in second.keywords] == ["Python"]
    # A different owner's rules are independent, even in the same Telegram chat.
    other = Owner(telegram_user_id=43, notification_chat_id=43)
    async with sessions() as session, session.begin():
        session.add(other)
    other_card = await groups.create_or_reactivate(
        other.id,
        ResolvedGroup(-100123, "Other user", None),
        parse_keyword_input("Java"),
        scan_history=False,
    )
    await groups.append_keywords(owner_id, first_id, parse_keyword_input("remote & backend"))
    for group_id in (first_id, second.id):
        card = await groups.get_card(owner_id, group_id)
        assert {k.value for k in card.keywords} == {"Python", "remote & backend"}
    monitored = await MonitoringRepository(sessions).active_groups_for_chat(
        -100123, datetime.now(UTC)
    )
    assert {g.id: {k.value for k in g.keywords} for g in monitored} == {
        first_id: {"Python", "remote & backend"},
        other_card.id: {"Java"},
    }
    replacement = await groups.replace_keywords(owner_id, second.id, parse_keyword_input("Rust"))
    assert await groups.delete_keyword(other.id, replacement.keywords[0].id) is None
    assert await groups.replace_keywords(other.id, first_id, parse_keyword_input("wrong")) is None
    assert [k.value for k in (await groups.get_card(owner_id, first_id)).keywords] == ["Rust"]
    assert [k.value for k in (await groups.get_card(other.id, other_card.id)).keywords] == ["Java"]
    # Existing jobs retain their snapshot; subsequent scans use the shared current list.
    async with sessions() as session:
        initial = await session.scalar(select(ScanJob).where(ScanJob.group_id == second.id))
        assert initial.keyword_snapshot == [{"value": "Python", "terms": ["python"]}]
    job, created = await ScanRepository(sessions).create_manual_scan(owner_id, first_id)
    assert created and job.keyword_snapshot == [{"value": "Rust", "terms": ["rust"]}]
    await groups.remove_group(owner_id, first_id)
    await groups.remove_group(owner_id, second.id)
    assert [k.value for k in await groups.get_keywords(owner_id)] == ["Rust"]
    reactivated = await groups.create_or_reactivate(
        owner_id,
        ResolvedGroup(-100123, "Jobs again", None),
        parse_keyword_input("stale setup draft"),
        scan_history=True,
    )
    assert [k.value for k in reactivated.keywords] == ["Rust"]


@pytest.mark.asyncio
async def test_last_shared_keyword_pauses_groups_and_preserves_access_lost(sessions):
    owner_id, first_id = await _create_group(sessions)
    groups = GroupRepository(sessions)
    second = await groups.create_or_reactivate(
        owner_id, ResolvedGroup(-100456, "Other jobs", None), scan_history=False
    )
    async with sessions() as session, session.begin():
        (await session.get(TrackedGroup, second.id)).status = GroupStatus.ACCESS_LOST
    card = await groups.get_card(owner_id, first_id)
    await groups.delete_keyword(owner_id, card.keywords[0].id)
    assert (await groups.get_card(owner_id, first_id)).status == GroupStatus.PAUSED
    assert (await groups.get_card(owner_id, second.id)).status == GroupStatus.ACCESS_LOST
    assert (
        await MonitoringRepository(sessions).active_groups_for_chat(-100123, datetime.now(UTC))
        == []
    )
    restored = await groups.restore_access(
        owner_id, second.id, ResolvedGroup(-100456, "Other jobs", None)
    )
    assert restored.status == GroupStatus.PAUSED
    await groups.append_keywords(owner_id, first_id, parse_keyword_input("Rust"))
    assert (await groups.get_card(owner_id, first_id)).status == GroupStatus.ACTIVE
    assert (await groups.get_card(owner_id, second.id)).status == GroupStatus.ACTIVE


@pytest.mark.asyncio
async def test_concurrent_shared_keyword_appends_do_not_lose_rules(sessions):
    owner_id, group_id = await _create_group(sessions)
    groups = GroupRepository(sessions)
    await asyncio.gather(
        groups.append_keywords(owner_id, group_id, parse_keyword_input("Rust")),
        groups.append_keywords(owner_id, group_id, parse_keyword_input("Go")),
    )
    assert {k.value for k in await groups.get_keywords(owner_id)} == {"Python", "Rust", "Go"}


async def _migrate(sessions, revision, *, downgrade=False):
    def migrate(connection):
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        config.attributes["connection"] = connection
        (command.downgrade if downgrade else command.upgrade)(config, revision)

    async with sessions.kw["bind"].begin() as connection:
        await connection.run_sync(migrate)


@pytest.mark.asyncio
async def test_migration_merges_existing_lists_without_losing_rules(sessions):
    await _migrate(sessions, "0009_catalog_search", downgrade=True)
    owner = Owner(telegram_user_id=42)
    other = Owner(telegram_user_id=43)
    first = TrackedGroup(owner=owner, telegram_chat_id=-1001, title="First")
    second = TrackedGroup(
        owner=owner, telegram_chat_id=-1002, title="Second", status=GroupStatus.PAUSED
    )
    removed = TrackedGroup(
        owner=owner, telegram_chat_id=-1003, title="Removed", status=GroupStatus.REMOVED
    )
    separate = TrackedGroup(owner=other, telegram_chat_id=-1001, title="Separate")
    async with sessions() as session, session.begin():
        session.add_all([owner, other, first, second, removed, separate])
        await session.flush()
        rules = [
            (first.id, "Python", "python", '["python"]'),
            (second.id, "PYTHON", "python", '["python"]'),
            (first.id, "R&D", "r&d", '["r&d"]'),  # legacy literal
            (second.id, '"R&D"', '"r&d"', '["r&d"]'),
            (second.id, "R & D", "r & d", '["r", "d"]'),
            (first.id, "R & D", "r & d", '["r & d"]'),  # same display, different meaning
            (removed.id, "discarded", "discarded", '["discarded"]'),
            (separate.id, "Java", "java", '["java"]'),
            *[(second.id, f"rule{i}", f"rule{i}", f'["rule{i}"]') for i in range(49)],
        ]
        for group_id, value, normalized, terms in rules:
            await session.execute(
                text("""
                INSERT INTO keywords (id, group_id, value, normalized_value, terms)
                VALUES (:id, :group_id, :value, :normalized, CAST(:terms AS jsonb))
            """),
                dict(
                    id=uuid.uuid4(),
                    group_id=group_id,
                    value=value,
                    normalized=normalized,
                    terms=terms,
                ),
            )
    await _migrate(sessions, "head")
    groups = GroupRepository(sessions)
    keywords = await groups.get_keywords(owner.id)
    assert len(keywords) == 53  # The union can exceed 50; migration must not truncate it.
    assert {k.terms for k in keywords} >= {("r&d",), ("r", "d"), ("r & d",)}
    assert (await groups.get_card(owner.id, second.id)).status == GroupStatus.ACTIVE
    assert [k.value for k in await groups.get_keywords(other.id)] == ["Java"]
    with pytest.raises(KeywordInputError, match="не более 50"):
        await groups.append_keywords(owner.id, first.id, parse_keyword_input("extra"))
    # Re-adding existing rules is allowed even for an oversized migrated list.
    await groups.append_keywords(owner.id, first.id, parse_keyword_input("python"))
    assert len(await groups.get_keywords(owner.id)) == 53
    await _migrate(sessions, "0009_catalog_search", downgrade=True)
    async with sessions() as session:
        rows = (
            await session.execute(text("SELECT group_id, count(*) FROM keywords GROUP BY group_id"))
        ).all()
        assert dict(rows) == {first.id: 53, second.id: 53, separate.id: 1}
    await _migrate(sessions, "head")
    assert len(await groups.get_keywords(owner.id)) == 53


async def _setup_catalog(sessions):
    from tg_jobs_searcher.db.catalog_repository import CatalogRepository

    owner_id, group_id = await _create_group(sessions)
    catalog = CatalogRepository(sessions)
    await catalog.add_source(ResolvedGroup(-100123, "Shared jobs", "jobs"))
    await catalog.set_search(owner_id, parse_keyword_input("python & вакансия"))
    return catalog, owner_id, group_id


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_first", [True, False])
async def test_catalog_and_personal_group_share_one_delivery(sessions, catalog_first):
    catalog, owner_id, group_id = await _setup_catalog(sessions)
    monitoring = MonitoringRepository(sessions)
    now = datetime.now(UTC)
    arguments = dict(
        telegram_chat_id=-100123, telegram_message_id=88, message_date=now, text="Python вакансия"
    )
    local_args = dict(
        group_id=group_id,
        telegram_message_id=88,
        message_date=now,
        matched_keywords=["Python"],
        payload="local match",
    )
    if catalog_first:
        assert await catalog.process_message(**arguments) == 1
        assert not await monitoring.record_live_match(**local_args)
    else:
        assert await monitoring.record_live_match(**local_args)
        assert await catalog.process_message(**arguments) == 0
    assert await catalog.process_message(**arguments) == 0
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NotificationOutbox)) == 1
        match = await session.scalar(select(MessageMatch))
        assert match.owner_id == owner_id and match.group_id == group_id
        assert match.catalog_source_id is not None
    # Removing the personal route must preserve delivery through the catalog.
    await GroupRepository(sessions).remove_group(owner_id, group_id)
    notifications = NotificationRepository(sessions)
    deliveries = await notifications.claim_batch()
    assert len(deliveries) == 1
    assert await notifications.is_deliverable(deliveries[0])
    await catalog.stop_search(owner_id)
    assert not await notifications.is_deliverable(deliveries[0])


@pytest.mark.asyncio
async def test_catalog_filters_sources_dates_and_individual_keywords(sessions):
    catalog, owner_id, _ = await _setup_catalog(sessions)
    from tg_jobs_searcher.db.models import CatalogSource

    other = Owner(telegram_user_id=43, notification_chat_id=43, is_enabled=True)
    async with sessions() as session, session.begin():
        session.add(other)
        await session.flush()
    await catalog.set_search(other.id, parse_keyword_input("react"))
    now = datetime.now(UTC)
    for chat_id, message_id, date, text_value in [
        (-100999, 1, now, "Python вакансия"),
        (-100123, 2, now - timedelta(hours=1), "Python вакансия"),
        (-100123, 3, now, "Python developer"),
    ]:
        assert (
            await catalog.process_message(
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                message_date=date,
                text=text_value,
            )
            == 0
        )
    assert (
        await catalog.process_message(
            telegram_chat_id=-100123,
            telegram_message_id=4,
            message_date=now,
            text="Python вакансия",
        )
        == 1
    )
    assert (
        await catalog.process_message(
            telegram_chat_id=-100123,
            telegram_message_id=5,
            message_date=now,
            text="React developer",
        )
        == 1
    )
    async with sessions() as session:
        matches = list(
            await session.scalars(select(MessageMatch).order_by(MessageMatch.telegram_message_id))
        )
        assert [m.owner_id for m in matches] == [owner_id, other.id]
        assert await session.scalar(select(func.count()).select_from(CatalogSource)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["search", "source", "user"])
async def test_catalog_revocation_cancels_pending_notifications(sessions, stop):
    from tg_jobs_searcher.db.owner_repository import OwnerRepository

    catalog, owner_id, _ = await _setup_catalog(sessions)
    args = dict(
        telegram_chat_id=-100123,
        telegram_message_id=9,
        message_date=datetime.now(UTC),
        text="Python вакансия",
    )
    assert await catalog.process_message(**args) == 1
    if stop == "search":
        await catalog.stop_search(owner_id)
    elif stop == "source":
        assert await catalog.remove_source(-100123)
    else:
        assert await OwnerRepository(sessions, 1).revoke_user(42)
    assert await catalog.process_message(**args) == 0
    assert await NotificationRepository(sessions).claim_batch() == []
    async with sessions() as session:
        assert (await session.scalar(select(NotificationOutbox))).status == WorkStatus.CANCELLED


@pytest.mark.asyncio
async def test_catalog_import_queue_survives_restart_and_flood_wait(sessions):
    from tg_jobs_searcher.db.catalog_repository import CatalogRepository
    from tg_jobs_searcher.db.models import CatalogImport

    catalog = CatalogRepository(sessions)
    assert await catalog.enqueue_imports(["@first", "@second", "@first"]) == 2
    assert await catalog.enqueue_imports(["@first"]) == 0
    first = await catalog.claim_import()
    assert first is not None
    restarted = CatalogRepository(sessions)
    await restarted.requeue_imports()
    job = await restarted.claim_import()
    assert job.id == first.id
    await restarted.finish_import(job.id, error="Flood wait", retry_after=120)
    assert await restarted.claim_import() is None
    assert await restarted.enqueue_imports(["@new_during_cooldown"]) == 1
    assert await restarted.claim_import() is None
    async with sessions() as session:
        rows = list(await session.scalars(select(CatalogImport)))
        assert all(row.status == "pending" for row in rows)
        assert all(
            row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=100)
            for row in rows
            if row.reference != "@new_during_cooldown"
        )
