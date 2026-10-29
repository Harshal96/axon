from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from axon.workers.java.resolver import resolve_import_to_target


@pytest.mark.asyncio
@patch("axon.workers.java.resolver.async_session")
async def test_resolve_exact_match(mock_session_ctx):
    mock_session = AsyncMock()
    mock_result = AsyncMock()
    mock_result.single.return_value = {"label": "//some:target"}
    mock_session.run.return_value = mock_result

    # async_session() returns an async context manager
    mock_session_ctx.return_value.__aenter__.return_value = mock_session

    res = await resolve_import_to_target("com.company.payment.PaymentGateway", False)
    assert res == ("//some:target", "EXTRACTED")
