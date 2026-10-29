"""System-level token reduction benchmark checks."""

from __future__ import annotations

import pytest

from axon.ops.subgraph import subgraph

pytestmark = pytest.mark.tier3


@pytest.mark.asyncio
async def test_subgraph_token_estimate_under_budget(system_workspace) -> None:
    result = await subgraph(["//services/payment:payment_lib"], depth=2, token_budget=4000)
    assert result.token_estimate <= 4000
    assert result.token_estimate > 0


@pytest.mark.asyncio
async def test_architecture_summary_is_non_empty(system_workspace) -> None:
    result = await subgraph(["//services/payment:payment_lib"], depth=2)
    assert len(result.architecture_summary) > 50
    assert "payment" in result.architecture_summary.lower()


@pytest.mark.asyncio
async def test_generated_code_warning_in_subgraph(system_workspace) -> None:
    result = await subgraph(["//services/payment:generate_payment_client_py"])
    assert len(result.generated_code) >= 1
    assert any("DO NOT EDIT" in item.warning.upper() for item in result.generated_code)
