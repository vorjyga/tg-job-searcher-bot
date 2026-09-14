import uuid

from tg_jobs_searcher.bot.management import (
    _callback_uuid,
    _format_keyword_values,
    _keywords_from_draft,
    _mode_keyboard,
    _parse_mode_callback,
)


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


def test_card_keywords_are_limited_to_telegram_message_size() -> None:
    text = _format_keyword_values(["x" * 500 for _ in range(50)])

    assert len(text) < 4096
    assert "и ещё" in text
