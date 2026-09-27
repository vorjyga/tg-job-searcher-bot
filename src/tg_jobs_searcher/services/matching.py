"""Unicode-aware matching for configured words, phrases and technical keywords."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MatchableKeyword:
    value: str
    normalized_value: str


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
        if _keyword_pattern(keyword.normalized_value).search(normalised_text)
    ]


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
