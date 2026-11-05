"""Unicode-aware matching for configured words, phrases and technical keywords."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from tg_jobs_searcher.services.keyword_expressions import (
    KeywordInputError,
    expression_from_data,
)


@dataclass(frozen=True, slots=True)
class MatchableKeyword:
    value: str
    normalized_value: str
    terms: tuple[str, ...] | None = None
    expression: dict[str, Any] | None = None


def normalise_search_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def find_matching_keywords(text: str, keywords: list[MatchableKeyword]) -> list[str]:
    """Return display values for every keyword found in a message or media caption."""
    normalised_text = normalise_search_text(text)
    if not normalised_text:
        return []
    return [keyword.value for keyword in keywords if _matches_rule(normalised_text, keyword)]


def _matches_rule(text: str, keyword: MatchableKeyword) -> bool:
    def contains(term: str) -> bool:
        return bool(_keyword_pattern(term).search(text))

    if keyword.expression is not None:
        return expression_from_data(keyword.expression).matches(contains)
    return all(contains(term) for term in (keyword.terms or (keyword.normalized_value,)))


def keywords_from_snapshot(snapshot: list[object]) -> list[MatchableKeyword]:
    """Read both structured rules and pre-upgrade literal keyword snapshots."""
    result: list[MatchableKeyword] = []
    for item in snapshot:
        if isinstance(item, str):
            result.append(MatchableKeyword(value=item, normalized_value=item))
        elif isinstance(item, dict):
            value = item.get("value")
            terms = item.get("terms")
            expression = item.get("expression")
            if (
                isinstance(value, str)
                and isinstance(terms, list)
                and (terms or expression is not None)
                and all(isinstance(term, str) and term for term in terms)
            ):
                if expression is not None:
                    try:
                        expression_from_data(expression)
                    except KeywordInputError:
                        continue
                result.append(
                    MatchableKeyword(
                        value=value,
                        normalized_value=value.casefold(),
                        terms=tuple(terms),
                        expression=expression,
                    )
                )
    return result


@lru_cache(maxsize=4096)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword)
    suffix = r"(?!\w)"
    # Technical tokens need the punctuation to be terminal, too: C++ does not match C+++.
    if keyword.endswith("+"):
        suffix = r"(?![\w+])"
    elif keyword.endswith("#"):
        suffix = r"(?![\w#])"
    elif keyword.endswith("."):
        suffix = r"(?![\w.])"
    return re.compile(rf"(?<!\w){escaped}{suffix}")
