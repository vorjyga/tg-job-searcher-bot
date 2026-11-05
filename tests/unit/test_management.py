import uuid
from datetime import UTC, datetime

from tg_jobs_searcher.bot.management import (
    RULE_HELP,
    _callback_uuid,
    _format_keyword_values,
    _format_scan_statuses,
    _keywords_from_draft,
    _mode_keyboard,
    _parse_mode_callback,
)
from tg_jobs_searcher.db.models import ScanJobType, WorkStatus
from tg_jobs_searcher.db.repositories import ScanJobStatus


def test_mode_callback_uses_nonce_and_is_within_telegram_limit() -> None:
    nonce = uuid.uuid4().hex
    callback_data = _mode_keyboard(nonce).inline_keyboard[0][0].callback_data

    assert callback_data == f"setup:mode:{nonce}:history"
    assert len(callback_data) <= 64
    assert _parse_mode_callback(callback_data) == (nonce, "history")


def test_rejects_invalid_callback_uuid() -> None:
    assert _callback_uuid("group:open:not-a-uuid", "group:open:") is None


def test_restores_valid_keyword_draft() -> None:
    keywords = _keywords_from_draft(
        {
            "keywords": [
                {"value": "Python", "normalized_value": "python"},
                {"value": "backend developer", "normalized_value": "backend developer"},
            ]
        }
    )

    assert [keyword.normalized_value for keyword in keywords] == ["python", "backend developer"]


def test_restores_rule_draft_with_literal_ampersand() -> None:
    keywords = _keywords_from_draft(
        {
            "keywords": [
                {
                    "value": '"R&D" & developer',
                    "normalized_value": '"r&d" & developer',
                    "terms": ["r&d", "developer"],
                }
            ]
        }
    )

    assert keywords[0].terms == ("r&d", "developer")


def test_group_prompt_explains_operators_and_literal_ampersand() -> None:
    assert "Запятая означает ИЛИ" in RULE_HELP
    assert "& означает И" in RULE_HELP
    assert '"R&D"' in RULE_HELP


def test_card_keywords_are_limited_to_telegram_message_size() -> None:
    text = _format_keyword_values(["x" * 500 for _ in range(50)])

    assert len(text) < 4096
    assert "и ещё" in text


def test_formats_scan_status_for_status_command() -> None:
    text = _format_scan_statuses(
        [
            ScanJobStatus(
                group_title="Python jobs",
                job_type=ScanJobType.MANUAL_SEVEN_DAYS,
                status=WorkStatus.RETRY,
                messages_checked=120,
                matches_found=4,
                range_end=datetime(2026, 9, 15, tzinfo=UTC),
                next_attempt_at=None,
            )
        ]
    )

    assert "повторный поиск" in text
    assert "будет повторено" in text
    assert "совпадений 4" in text


def test_restores_boolean_draft_without_losing_expression() -> None:
    from tg_jobs_searcher.services.keywords import parse_keyword_input

    rules = parse_keyword_input("!#resume & (react || vue)")
    draft = {
        "keywords": [
            {**rule.snapshot(), "normalized_value": rule.normalized_value} for rule in rules
        ]
    }
    assert _keywords_from_draft(draft) == rules


def test_restores_old_draft_with_new_reserved_symbols_as_literals() -> None:
    keywords = _keywords_from_draft(
        {
            "keywords": [
                {
                    "value": "!resume & React (Native)",
                    "normalized_value": "!resume & react (native)",
                    "terms": ["!resume", "react (native)"],
                }
            ]
        }
    )
    assert keywords[0].expression is None
    assert keywords[0].terms == ("!resume", "react (native)")
    assert keywords[0].value == '"!resume" & "react (native)"'


def test_prompt_explains_boolean_groups_and_exclusion() -> None:
    assert "!#resume & (angular || vue || react || frontend)" in RULE_HELP
    assert "сначала !, затем &, затем ||" in RULE_HELP
    assert "НЕ" in RULE_HELP


def test_current_literal_draft_preserves_display_casing() -> None:
    from tg_jobs_searcher.services.keywords import parse_keyword_input

    rules = parse_keyword_input('"React (Native)" & "!Resume"')
    assert _keywords_from_draft({"keywords": [rule.snapshot() for rule in rules]}) == rules
