"""Read keyword queries without silently accepting Telegram spoiler formatting."""

from aiogram.types import Message

from tg_jobs_searcher.services.keywords import KeywordInput, KeywordInputError, parse_keyword_input


def parse_keyword_message(message: Message, text: str | None = None) -> list[KeywordInput]:
    # Telegram clients can consume pairs of || as spoiler delimiters before the
    # message reaches us. The text alone no longer contains the intended query.
    # Reject instead of guessing whether each spoiler boundary meant an OR.
    if any(entity.type == "spoiler" for entity in message.entities or []):
        raise KeywordInputError(
            "В запросе есть форматирование «спойлер»: Telegram мог удалить знаки ||. "
            "Замените || на запятые внутри скобок, например: "
            "!#resume & (angular, vue, react, frontend). "
            "Или отправьте исходный запрос целиком как код (моноширинный текст)."
        )
    value = message.text if text is None else text
    return parse_keyword_input(value or "")
