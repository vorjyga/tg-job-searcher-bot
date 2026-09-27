"""Unicode-aware matching for configured words, phrases and technical keywords."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MatchableKeyword:
    value: str
    normalized_value: str
    terms: tuple[str, ...] | None = None


def normalise_search_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def find_matching_keywords(text: str, keywords: list[MatchableKeyword]) -> list[str]:
    """Return display values for every keyword found in a message or media caption."""
    normalised_text = normalise_search_text(text)
    if not normalised_text:
        return []
    return [
        keyword.value
        for keyword in keywords
        if all(
            _keyword_pattern(term).search(normalised_text)
            for term in (keyword.terms or (keyword.normalized_value,))
        )
    ]


def keywords_from_snapshot(snapshot: list[object]) -> list[MatchableKeyword]:
    """Read both structured rules and pre-upgrade literal keyword snapshots."""
    result: list[MatchableKeyword] = []
    for item in snapshot:
        if isinstance(item, str):
            result.append(MatchableKeyword(value=item, normalized_value=item))
        elif isinstance(item, dict):
            value = item.get("value")
            terms = item.get("terms")
            if (
                isinstance(value, str)
                and isinstance(terms, list)
                and terms
                and all(isinstance(term, str) and term for term in terms)
            ):
                result.append(
                    MatchableKeyword(value=value, normalized_value=value.casefold(), terms=tuple(terms))
                )
    return result


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
