import pytest

from tg_jobs_searcher.services.keywords import parse_keyword_input
from tg_jobs_searcher.services.matching import (
    MatchableKeyword,
    find_matching_keywords,
    keywords_from_snapshot,
)


def test_matches_words_phrases_cyrillic_and_technical_tokens() -> None:
    keywords = [
        MatchableKeyword("Python", "python"),
        MatchableKeyword("backend developer", "backend developer"),
        MatchableKeyword("#Вакансия", "#вакансия"),
        MatchableKeyword("C++", "c++"),
        MatchableKeyword(".NET", ".net"),
    ]

    matches = find_matching_keywords(
        "#ВАКАНСИЯ: нужен Python backend\n developer с C++ и .NET.", keywords
    )

    assert matches == ["Python", "backend developer", "#Вакансия", "C++", ".NET"]


def test_does_not_match_a_keyword_inside_a_larger_word() -> None:
    keywords = [MatchableKeyword("Java", "java"), MatchableKeyword("C++", "c++")]

    assert find_matching_keywords("Javascript and C+++ are different", keywords) == []


def test_and_within_each_rule_or_between_rules() -> None:
    rules = [
        MatchableKeyword(rule.value, rule.normalized_value, rule.terms)
        for rule in parse_keyword_input("frontend & #вакансия, vue 3 & react 21")
    ]

    assert find_matching_keywords("frontend developer role", rules) == []
    assert find_matching_keywords("#ВАКАНСИЯ для frontend developer", rules) == [
        "frontend & #вакансия"
    ]
    assert find_matching_keywords("Vue   3 or react 21 developer", rules) == ["vue 3 & react 21"]
    assert find_matching_keywords("Vue 3 developer, React 20", rules) == []


def test_literal_ampersand_and_legacy_snapshot_stay_literal() -> None:
    rules = [
        MatchableKeyword(rule.value, rule.normalized_value, rule.terms)
        for rule in parse_keyword_input('"R&D" & developer')
    ]
    assert find_matching_keywords("R&D developer", rules) == ['"R&D" & developer']
    assert find_matching_keywords("R and D developer", rules) == []

    legacy = keywords_from_snapshot(["r&d"])
    assert find_matching_keywords("R&D developer", legacy) == ["r&d"]
    assert find_matching_keywords("R and D developer", legacy) == []


def test_structured_snapshot_uses_all_terms() -> None:
    snapshot = [{"value": "frontend & #вакансия", "terms": ["frontend", "#вакансия"]}]
    rules = keywords_from_snapshot(snapshot)

    assert find_matching_keywords("frontend role", rules) == []
    assert find_matching_keywords("#вакансия frontend role", rules) == ["frontend & #вакансия"]


def boolean_rules(value):
    return keywords_from_snapshot([rule.snapshot() for rule in parse_keyword_input(value)])


@pytest.mark.parametrize(
    "text, matches",
    [
        ("Angular developer", True),
        ("Vue developer", True),
        ("REACT developer", True),
        ("frontend developer", True),
        ("Angular Vue React frontend", True),
        ("#resume React developer", False),
        ("Vue developer #RESUME", False),
        ("Backend Python developer", False),
        ("reactive development", False),
        ("", False),
        (" \n ", False),
    ],
)
def test_requested_resume_exclusion(text, matches) -> None:
    rules = boolean_rules("!#resume & (angular || vue || react || frontend)")
    assert bool(find_matching_keywords(text, rules)) is matches


@pytest.mark.parametrize(
    "query, text, matches",
    [
        ("react || vue & remote", "React onsite", True),
        ("react || vue & remote", "Vue onsite", False),
        ("react || vue & remote", "Vue remote", True),
        ("(react || vue) & remote", "React onsite", False),
        ("(react || vue) & remote", "React remote", True),
        ("!react & vue", "React Vue", False),
        ("!react & vue", "Vue", True),
        ("!(#resume || #резюме) & frontend", "frontend #резюме", False),
        ("!(#resume || #резюме) & frontend", "frontend vacancy", True),
        ("!!react", "react", True),
        ("!!react", "vue", False),
        ("!ищу работу & react", "ИЩУ\n РАБОТУ React", False),
        ("!ищу работу & react", "React вакансия", True),
        ("!#resume", "backend job", True),
        ("!#resume", "#resumes", True),
        ("!#resume", "#resume", False),
        ("!resume & angular, vue", "resume Vue", True),
        ('"!resume" & "(react || vue)"', "!resume (React || Vue)", True),
        ('"!resume" & "(react || vue)"', "React", False),
    ],
)
def test_boolean_precedence_groups_phrases_and_literals(query, text, matches) -> None:
    assert bool(find_matching_keywords(text, boolean_rules(query))) is matches


def test_legacy_terms_with_new_reserved_symbols_remain_literal() -> None:
    snapshot = [
        "!resume",
        {"value": "React (Native)", "terms": ["react (native)"]},
        {"value": "a || b", "terms": ["a || b"]},
    ]
    rules = keywords_from_snapshot(snapshot)
    assert find_matching_keywords("Vue vacancy", rules) == []
    assert find_matching_keywords("!resume React (Native) a || b", rules) == [
        "!resume",
        "React (Native)",
        "a || b",
    ]


@pytest.mark.parametrize(
    "expression", [{}, {"op": []}, {"op": "not", "args": []}, {"op": "term", "value": ""}]
)
def test_invalid_snapshot_expression_is_not_treated_as_a_positive_rule(expression) -> None:
    snapshot = [{"value": "react", "terms": ["react"], "expression": expression}]
    assert keywords_from_snapshot(snapshot) == []
