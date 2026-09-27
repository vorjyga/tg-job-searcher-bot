from tg_jobs_searcher.services.matching import MatchableKeyword, find_matching_keywords


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
