"""Run repository workflows against migrations in an isolated PostgreSQL schema."""

from __future__ import annotations

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
            Keyword(group_id=group.id, value="Python", normalized_value="python", terms=["python"])
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
            == "0008_bot_pause"
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
                    group_id=group_id,
                    value=f"keyword{index}",
                    normalized_value=f"keyword{index}",
                    terms=[f"keyword{index}"],
                )
            )

    with pytest.raises(KeywordInputError, match="не более 50"):
        await groups.append_keywords(owner_id, group_id, parse_keyword_input("extra"))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Keyword)) == 50
