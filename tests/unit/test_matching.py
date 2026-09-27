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
    assert find_matching_keywords("Vue   3 or react 21 developer", rules) == [
        "vue 3 & react 21"
    ]
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
    assert find_matching_keywords("#вакансия frontend role", rules) == [
        "frontend & #вакансия"
    ]
