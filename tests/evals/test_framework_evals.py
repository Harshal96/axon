"""Pytest entry point for the language/framework extraction evals."""

from __future__ import annotations

import pytest

from axon.v2.indexer import PARSERS
from tests.evals.harness import EvalCase, evaluate_case, load_cases, load_matrix

pytestmark = pytest.mark.tier1

CASES = load_cases()

EXPECTED_SUPPORTED = {
    ("python", "fastapi"),
    ("python", "django"),
    ("python", "flask"),
    ("python", "aiohttp"),
    ("java", "jaxrs"),
    ("java", "spring"),
    ("java", "vertx"),
    ("java", "jaxrs"),
    ("go", "go-http"),
}
EXPECTED_KNOWN_GAPS = {("typescript", "express")}
_EXTENSION_LANGUAGES = {".py": "python", ".java": "java", ".go": "go"}


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_framework_eval(case: EvalCase) -> None:
    result = evaluate_case(case)
    assert result.passed, "\n".join(result.failures)


def test_eval_matrix_covers_every_supported_framework() -> None:
    matrix = load_matrix()
    supported = {(str(row["language"]), str(row["framework"])) for row in matrix["supported"]}
    known_gaps = {(str(row["language"]), str(row["framework"])) for row in matrix["known_gaps"]}
    assert supported == EXPECTED_SUPPORTED
    assert known_gaps == EXPECTED_KNOWN_GAPS

    covered = {(case.language, case.framework) for case in CASES if case.support == "supported"}
    gap_cases = {(case.language, case.framework) for case in CASES if case.support == "known-gap"}
    assert covered == supported
    assert gap_cases == known_gaps


def test_every_parser_language_has_eval_coverage() -> None:
    parser_languages = {_EXTENSION_LANGUAGES.get(extension, extension) for extension in PARSERS}
    supported_languages = {language for language, _framework in EXPECTED_SUPPORTED}
    assert parser_languages == supported_languages, (
        "a parser language changed without updating the eval matrix: "
        f"parsers={sorted(parser_languages)}, evals={sorted(supported_languages)}"
    )
