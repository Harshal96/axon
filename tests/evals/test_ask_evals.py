import asyncio
from typing import Any

import pytest

from axon.v1.ask import DETERMINISTIC_INTENTS
from tests.evals.ask.harness import evaluate, load_cases


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_ask_contract(case: dict[str, Any]) -> None:
    result = asyncio.run(evaluate(case))
    assert result["passed"], "\n".join(result["failures"])


def test_ask_intent_coverage() -> None:
    assert DETERMINISTIC_INTENTS <= {c["expect"]["intent"] for c in load_cases()}


def test_response_intent_literals_have_contract_cases() -> None:
    import ast
    from pathlib import Path

    import axon.v1.ask as ask_module

    tree = ast.parse(Path(ask_module.__file__).read_text())
    intents = {
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "intent" and isinstance(keyword.value, ast.Constant)
    }
    assert intents <= {case["expect"]["intent"] for case in load_cases()}
