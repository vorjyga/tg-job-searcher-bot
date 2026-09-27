from sqlalchemy.dialects import postgresql

from tg_jobs_searcher.db.models import (
    GroupStatus,
    MatchSource,
    MessageMatch,
    NotificationOutbox,
    ScanJob,
    ScanJobType,
    TrackedGroup,
    WorkStatus,
)


def test_postgresql_enum_bind_and_result_match_migration_labels() -> None:
    dialect = postgresql.dialect()
    cases = [
        (TrackedGroup.status.type, GroupStatus.ACTIVE),
        (ScanJob.job_type.type, ScanJobType.RECOVERY),
        (ScanJob.status.type, WorkStatus.RETRY),
        (MessageMatch.source.type, MatchSource.HISTORY),
        (NotificationOutbox.status.type, WorkStatus.PENDING),
    ]

    for column_type, member in cases:
        bind = column_type.bind_processor(dialect)
        result = column_type.result_processor(dialect, None)
        assert bind is not None
        assert result is not None
        assert bind(member) == member.value
        assert result(member.value) is member
