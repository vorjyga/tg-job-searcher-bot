"""Validation and canonicalisation of user-managed keyword lists."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_KEYWORDS_PER_GROUP = 50
MAX_KEYWORD_LENGTH = 500
MAX_TERMS_PER_RULE = 20


class KeywordInputError(ValueError):
    """Raised when a keyword list cannot be stored safely."""


@dataclass(frozen=True, slots=True)
class KeywordInput:
    value: str
    normalized_value: str
    terms: tuple[str, ...]


def parse_keyword_input(value: str) -> list[KeywordInput]:
    """Parse OR-separated rules of AND-connected words and phrases."""
    value = value.strip()
    if not value:
        raise KeywordInputError("Введите хотя бы одно ключевое слово или фразу")
    result: list[KeywordInput] = []
    seen: set[str] = set()
    for raw_terms in _split_rules(value):
        if len(raw_terms) > MAX_TERMS_PER_RULE:
            raise KeywordInputError(
                f"В одном условии допускается не более {MAX_TERMS_PER_RULE} частей"
            )
        display_terms = [_normalise_display_value(term) for term in raw_terms]
        if any(not term for term in display_terms):
            raise KeywordInputError("Между операторами должно быть ключевое слово или фраза")
        display_value = " & ".join(_quote_if_needed(term) for term in display_terms)
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
        result.append(
            KeywordInput(
                value=display_value,
                normalized_value=normalized_value,
                terms=tuple(term.casefold() for term in display_terms),
            )
        )

    if not result:
        raise KeywordInputError("Введите хотя бы одно ключевое слово или фразу")
    if len(result) > MAX_KEYWORDS_PER_GROUP:
        raise KeywordInputError(
            f"За один раз можно сохранить не более {MAX_KEYWORDS_PER_GROUP} ключевых слов"
        )
    return result


def _split_rules(value: str) -> list[list[str]]:
    rules: list[list[str]] = []
    terms: list[str] = []
    buffer: list[str] = []
    in_quotes = False
    closed_quote = False
    index = 0
    while index < len(value):
        char = value[index]
        if in_quotes:
            if char == '"':
                if index + 1 < len(value) and value[index + 1] == '"':
                    buffer.append('"')
                    index += 1
                else:
                    in_quotes = False
                    closed_quote = True
            else:
                buffer.append(char)
        elif char == '"':
            if "".join(buffer).strip() or closed_quote:
                raise KeywordInputError("Кавычки должны охватывать ключ целиком")
            in_quotes = True
        elif char in ",&\n":
            term = "".join(buffer).strip()
            if not term:
                raise KeywordInputError("Между операторами должно быть ключевое слово или фраза")
            terms.append(term)
            buffer = []
            closed_quote = False
            if char != "&":
                rules.append(terms)
                terms = []
        else:
            if closed_quote and not char.isspace():
                raise KeywordInputError("После закрывающей кавычки нужен оператор")
            buffer.append(char)
        index += 1
    if in_quotes:
        raise KeywordInputError("Закройте кавычки в ключевой фразе")
    term = "".join(buffer).strip()
    if not term:
        raise KeywordInputError("После оператора должно быть ключевое слово или фраза")
    terms.append(term)
    rules.append(terms)
    return rules


def _quote_if_needed(term: str) -> str:
    if any(char in term for char in ',&"'):
        return '"' + term.replace('"', '""') + '"'
    return term


def _normalise_display_value(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
