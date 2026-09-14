"""Validation and canonicalisation of user-managed keyword lists."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_KEYWORDS_PER_GROUP = 50
MAX_KEYWORD_LENGTH = 500


class KeywordInputError(ValueError):
    """Raised when a keyword list cannot be stored safely."""


@dataclass(frozen=True, slots=True)
class KeywordInput:
    value: str
    normalized_value: str


def parse_keyword_input(value: str) -> list[KeywordInput]:
    """Parse comma or newline separated words and phrases into unique keywords."""
    candidates = value.replace("\n", ",").split(",")
    result: list[KeywordInput] = []
    seen: set[str] = set()
    for candidate in candidates:
        display_value = _normalise_display_value(candidate)
        if not display_value:
            continue
        if len(display_value) > MAX_KEYWORD_LENGTH:
            raise KeywordInputError(
                f"Каждое ключевое слово или фраза должны быть не длиннее {MAX_KEYWORD_LENGTH} символов"
            )
        normalized_value = display_value.casefold()
        if normalized_value in seen:
            continue
        seen.add(normalized_value)
        result.append(KeywordInput(value=display_value, normalized_value=normalized_value))

    if not result:
        raise KeywordInputError("Введите хотя бы одно ключевое слово или фразу")
    if len(result) > MAX_KEYWORDS_PER_GROUP:
        raise KeywordInputError(
            f"За один раз можно сохранить не более {MAX_KEYWORDS_PER_GROUP} ключевых слов"
        )
    return result


def _normalise_display_value(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
