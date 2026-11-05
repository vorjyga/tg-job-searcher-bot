"""Regression checks for literal keys which predate Boolean operators."""

import runpy
from pathlib import Path

import pytest

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0011_boolean_keywords.py"
quote_legacy = runpy.run_path(str(MIGRATION))["_quote_new_operators"]


@pytest.mark.parametrize(
    "value, terms, expected",
    [
        ("!React", ["!react"], '"!React"'),
        ('"!React"', ["!react"], '"!React"'),
        ("React (Native)", ["react (native)"], '"React (Native)"'),
        ("a || b", ["a || b"], '"a || b"'),
        ("!resume & React (Native)", ["!resume", "react (native)"], '"!resume" & "React (Native)"'),
        ('"R&D" & !resume', ["r&d", "!resume"], '"R&D" & "!resume"'),
        (
            '"a ""quote""" & React (Native)',
            ['a "quote"', "react (native)"],
            '"a ""quote""" & "React (Native)"',
        ),
        ("Python & remote", ["python", "remote"], "Python & remote"),
    ],
)
def test_quotes_only_newly_reserved_literal_parts(value, terms, expected):
    assert quote_legacy(value, terms) == expected
