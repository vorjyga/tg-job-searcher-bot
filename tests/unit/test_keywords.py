import pytest

from tg_jobs_searcher.services.keywords import (
    MAX_KEYWORDS_PER_GROUP,
    KeywordInputError,
    parse_keyword_input,
)


def test_parses_deduplicates_and_normalises_keywords() -> None:
    keywords = parse_keyword_input(" Python ,backend   developer\nPYTHON, #Вакансия ")

    assert [(keyword.value, keyword.normalized_value) for keyword in keywords] == [
        ("Python", "python"),
        ("backend developer", "backend developer"),
        ("#Вакансия", "#вакансия"),
    ]


def test_rejects_empty_keyword_input() -> None:
    with pytest.raises(KeywordInputError, match="хотя бы одно"):
        parse_keyword_input(" , \n ")


def test_rejects_excessive_number_of_keywords() -> None:
    value = ",".join(f"keyword-{index}" for index in range(MAX_KEYWORDS_PER_GROUP + 1))

    with pytest.raises(KeywordInputError, match="не более"):
        parse_keyword_input(value)
