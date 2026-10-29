from __future__ import annotations

import warnings

import pytest

from axon.v2.facts import MAX_DIAGNOSTICS_PER_FILE
from axon.v2.parse_python import _is_invalid_escape, parse_python

pytestmark = pytest.mark.tier1


def test_invalid_escapes_are_captured_not_printed():
    src = "PAT = '\\d+'\nPAT2 = '\\s'\nPAT3 = '\\{'\n\ndef target():\n    return 1\n"
    with warnings.catch_warnings(record=True) as outer:
        warnings.simplefilter("always")
        facts = parse_python("svc/app.py", src)
    assert outer == []
    assert [d.line for d in facts.diagnostics] == [1, 2, 3]
    assert all(d.message.startswith("invalid escape sequence") for d in facts.diagnostics)
    assert all(d.category for d in facts.diagnostics)
    assert "target" in {s.name for s in facts.symbols}


def test_is_invalid_escape_classification():
    assert _is_invalid_escape("invalid escape sequence '\\d'")
    assert not _is_invalid_escape('"is" with a literal. Did you mean "=="?')
    assert not _is_invalid_escape("")


def test_per_file_cap_and_truncation_count():
    lines = "".join(f"P{i} = '\\d'\n" for i in range(MAX_DIAGNOSTICS_PER_FILE + 5))
    facts = parse_python("svc/app.py", lines)
    assert len(facts.diagnostics) == MAX_DIAGNOSTICS_PER_FILE
    assert facts.diagnostics_truncated == 5


def test_syntax_error_becomes_one_diagnostic():
    facts = parse_python("svc/broken.py", "def broken(:\n")
    assert facts.symbols == []
    assert len(facts.diagnostics) == 1
    assert facts.diagnostics[0].category == "SyntaxError"
    assert facts.diagnostics[0].line == 1
