from tg_jobs_searcher.bot.handlers import format_groups
from tg_jobs_searcher.telegram.client import ResolvedGroup


def test_formats_accessible_groups_and_limits_output() -> None:
    groups = [
        ResolvedGroup(telegram_chat_id=-1001, title="Python Jobs", username="python_jobs"),
        ResolvedGroup(telegram_chat_id=-1002, title="Backend Jobs", username=None),
    ]

    result = format_groups(groups, limit=1)

    assert "Python Jobs" in result
    assert "Backend Jobs" not in result
    assert "и ещё 1" in result


def test_limits_group_message_to_telegram_safe_length() -> None:
    groups = [
        ResolvedGroup(telegram_chat_id=-1000 - index, title="x" * 255, username=None)
        for index in range(50)
    ]

    result = format_groups(groups)

    assert len(result) <= 4096
    assert "и ещё" in result
