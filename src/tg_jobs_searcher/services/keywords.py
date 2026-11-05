"""Validation and canonicalisation of user-managed keyword lists."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tg_jobs_searcher.services.keyword_expressions import (
    MAX_TERMS_PER_RULE as MAX_TERMS_PER_RULE,
)
from tg_jobs_searcher.services.keyword_expressions import (
    KeywordInputError as KeywordInputError,
)
from tg_jobs_searcher.services.keyword_expressions import (
    parse_expressions,
)

MAX_KEYWORDS_PER_USER = 50
MAX_KEYWORD_LENGTH = 500


@dataclass(frozen=True, slots=True)
class KeywordInput:
    value: str
    normalized_value: str
    terms: tuple[str, ...]
    # None preserves the literal/AND semantics of existing stored rules.
    expression: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {"value": self.value, "terms": list(self.terms)}
        if self.expression is not None:
            result["expression"] = self.expression
        return result


def parse_keyword_input(value: str) -> list[KeywordInput]:
    """Parse a list of Boolean rules separated by commas or newlines."""
    value = value.strip()
    if not value:
        raise KeywordInputError("Введите хотя бы одно ключевое слово или фразу")
    result: list[KeywordInput] = []
    seen: set[str] = set()
    for expression in parse_expressions(value):
        display_value = expression.render()
        if len(display_value) > MAX_KEYWORD_LENGTH:
            raise KeywordInputError(
                f"Каждое условие должно быть не длиннее {MAX_KEYWORD_LENGTH} символов"
            )
        normalized_value = display_value.casefold()
        if len(normalized_value) > MAX_KEYWORD_LENGTH:
            raise KeywordInputError("После нормализации условие слишком длинное")
        if normalized_value in seen:
            continue
        seen.add(normalized_value)
        terms = expression.literal_terms()
        result.append(
            KeywordInput(
                value=display_value,
                normalized_value=normalized_value,
                terms=terms if terms is not None else (),
                expression=expression.to_data() if terms is None else None,
            )
        )

    if len(result) > MAX_KEYWORDS_PER_USER:
        raise KeywordInputError(
            f"За один раз можно сохранить не более {MAX_KEYWORDS_PER_USER} ключевых слов"
        )
    return result
