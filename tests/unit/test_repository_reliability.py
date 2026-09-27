import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tg_jobs_searcher.db.models import GroupStatus, ScanJobType
from tg_jobs_searcher.db.repositories import GroupRepository
from tg_jobs_searcher.services.keywords import KeywordInputError, parse_keyword_input
from tg_jobs_searcher.telegram.client import ResolvedGroup


class FakeSession:
    def __init__(self, group, rows=()):
        self.group = group
        self.rows = rows
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return self

    async def scalar(self, _query):
        return self.group

    async def scalars(self, _query):
        return []

    async def execute(self, _query):
        return SimpleNamespace(all=lambda: self.rows)

    def add(self, value):
        self.added.append(value)


@pytest.mark.asyncio
async def test_restore_access_queues_scan_for_gap() -> None:
    owner_id = uuid.uuid4()
    group = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=owner_id,
        telegram_chat_id=-100123,
        status=GroupStatus.ACCESS_LOST,
        monitoring_started_at=datetime.now(UTC) - timedelta(days=10),
        title="Old title",
        username=None,
        access_lost_notified_at=datetime.now(UTC),
    )
    session = FakeSession(group)
    repository = GroupRepository(lambda: session)  # type: ignore[arg-type]
    repository.get_card = AsyncMock(return_value="card")  # type: ignore[method-assign]

    result = await repository.restore_access(
        owner_id, group.id, ResolvedGroup(-100123, "Current title", "jobs")
    )

    assert result == "card"
    assert group.status == GroupStatus.ACTIVE
    assert len(session.added) == 1
    job = session.added[0]
    assert job.job_type == ScanJobType.RECOVERY
    assert job.group_id == group.id
    assert job.range_end - job.range_start == timedelta(days=7)


@pytest.mark.asyncio
async def test_appending_keywords_respects_group_total_limit() -> None:
    group = SimpleNamespace(id=uuid.uuid4(), status=GroupStatus.ACTIVE, configuration_version=1)
    session = FakeSession(group, [(f"keyword{i}", [f"keyword{i}"]) for i in range(50)])
    repository = GroupRepository(lambda: session)  # type: ignore[arg-type]

    with pytest.raises(KeywordInputError, match="не более 50"):
        await repository.append_keywords(uuid.uuid4(), group.id, parse_keyword_input("Python"))

    assert session.added == []
    assert group.configuration_version == 1
