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
        parse_keyword_input("   ")


def test_rejects_excessive_number_of_keywords() -> None:
    value = ",".join(f"keyword-{index}" for index in range(MAX_KEYWORDS_PER_GROUP + 1))

    with pytest.raises(KeywordInputError, match="не более"):
        parse_keyword_input(value)


def test_parses_and_or_rules_with_unquoted_multiword_phrases() -> None:
    rules = parse_keyword_input("\nfrontend & #вакансия, vue 3 & react 21\n")

    assert [(rule.value, rule.terms) for rule in rules] == [
        ("frontend & #вакансия", ("frontend", "#вакансия")),
        ("vue 3 & react 21", ("vue 3", "react 21")),
    ]


def test_quoted_reserved_characters_are_literal_and_round_trip() -> None:
    rules = parse_keyword_input('"R&D" & developer, "sales, marketing", "a ""quote"""')

    assert [rule.terms for rule in rules] == [
        ("r&d", "developer"),
        ("sales, marketing",),
        ('a "quote"',),
    ]
    assert parse_keyword_input(", ".join(rule.value for rule in rules)) == rules


@pytest.mark.parametrize(
    "value",
    ["python &", "& python", "python,,java", '"R&D', '"R&D" developer', "foo\"bar"],
)
def test_rejects_malformed_rule(value: str) -> None:
    with pytest.raises(KeywordInputError):
        parse_keyword_input(value)
