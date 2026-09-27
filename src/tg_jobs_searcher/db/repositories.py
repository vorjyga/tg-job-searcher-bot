"""Compatibility imports for domain-specific PostgreSQL repositories."""

from tg_jobs_searcher.db.analytics_repository import AnalyticsRepository
from tg_jobs_searcher.db.conversation_repository import ConversationRepository
from tg_jobs_searcher.db.group_repository import GroupRepository
from tg_jobs_searcher.db.monitoring_repository import MonitoringRepository
from tg_jobs_searcher.db.notification_repository import NotificationRepository
from tg_jobs_searcher.db.owner_repository import OwnerRepository
from tg_jobs_searcher.db.repository_types import (
    ON_DEMAND_REPORT_TIMEZONE,
    ConversationStep,
    DailyAnalytics,
    Delivery,
    GroupCard,
    GroupSummary,
    KeywordSummary,
    MonitoredGroup,
    ScanCompletion,
    ScanJobLease,
    ScanJobStatus,
    ScanMatch,
    ScanProgress,
    due_report_day,
    today_window_utc3,
)
from tg_jobs_searcher.db.scan_repository import ScanRepository

__all__ = [
    "AnalyticsRepository", "ConversationRepository", "ConversationStep",
    "DailyAnalytics", "Delivery", "GroupCard", "GroupRepository", "GroupSummary",
    "KeywordSummary", "MonitoredGroup", "MonitoringRepository", "NotificationRepository",
    "ON_DEMAND_REPORT_TIMEZONE", "OwnerRepository", "ScanCompletion", "ScanJobLease",
    "ScanJobStatus", "ScanMatch", "ScanProgress", "ScanRepository", "due_report_day",
    "today_window_utc3",
]
