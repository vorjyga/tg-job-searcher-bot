import pytest

from tg_jobs_searcher.services.keywords import (
    MAX_KEYWORDS_PER_USER,
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
    value = ",".join(f"keyword-{index}" for index in range(MAX_KEYWORDS_PER_USER + 1))

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
    ["python &", "& python", "python,,java", '"R&D', '"R&D" developer', 'foo"bar'],
)
def test_rejects_malformed_rule(value: str) -> None:
    with pytest.raises(KeywordInputError):
        parse_keyword_input(value)


@pytest.mark.parametrize(
    "value, expected",
    [
        (
            "!#resume&(angular||vue||react||frontend)",
            "!#resume & (angular || vue || react || frontend)",
        ),
        ("react || vue & remote", "react || vue & remote"),
        ("!(react || vue) & remote", "!(react || vue) & remote"),
        ("a & (b & c)", "a & b & c"),
        ("a || (b || c)", "a || b || c"),
        ("(a, b) & !c", "(a || b) & !c"),
        ("!!react", "!!react"),
        ('"hello!" & "React (Native)"', '"hello!" & "React (Native)"'),
    ],
)
def test_boolean_rules_are_canonical_and_round_trip(value, expected) -> None:
    rules = parse_keyword_input(value)
    assert len(rules) == 1
    assert rules[0].value == expected
    assert parse_keyword_input(expected) == rules


def test_boolean_alternatives_stay_one_rule_and_lists_stay_separate() -> None:
    rules = parse_keyword_input("!resume & (Angular || Vue), Python\nRust")
    assert len(rules) == 3
    assert rules[0].expression is not None
    assert rules[0].expression["op"] == "and"
    assert rules[0].terms == ()
    assert rules[1].terms == ("python",)
    assert rules[1].expression is None


@pytest.mark.parametrize(
    "value",
    [
        "!",
        "a ||",
        "|| a",
        "a || || b",
        "a | b",
        "a && b",
        "()",
        "(a",
        "a)",
        "(a))",
        "a(b)",
        "a !b",
        "a (b)",
        "a & !",
        "!(a,)",
        "(,a)",
        '""',
        '"a"b',
        'a"b"',
        "a,\n b",
    ],
)
def test_rejects_malformed_boolean_expression(value) -> None:
    with pytest.raises(KeywordInputError):
        parse_keyword_input(value)


def test_expression_limits_include_negations_and_alternatives() -> None:
    with pytest.raises(KeywordInputError, match="не более 20"):
        parse_keyword_input(" || ".join(f"!term{i}" for i in range(21)))
    with pytest.raises(KeywordInputError, match="вложенность"):
        parse_keyword_input("!" * 1000 + "react")
    with pytest.raises(KeywordInputError, match="вложенность"):
        parse_keyword_input("(" * 1000 + "react" + ")" * 1000)
    with pytest.raises(KeywordInputError, match="500"):
        parse_keyword_input("!" + "x" * 500)


def test_unicode_normalization_does_not_turn_literal_symbols_into_operators() -> None:
    rules = parse_keyword_input("！react")
    assert rules[0].terms == ("!react",)
    assert rules[0].value == '"!react"'
    assert parse_keyword_input(rules[0].value) == rules
