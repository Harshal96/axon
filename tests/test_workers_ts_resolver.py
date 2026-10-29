from unittest.mock import AsyncMock, patch

import pytest

from axon.workers.ts.resolver import resolve_import


@pytest.mark.asyncio
@patch("axon.workers.ts.resolver.async_session")
async def test_resolve_relative(mock_session_ctx):
    mock_session = AsyncMock()
    mock_result = AsyncMock()
    mock_result.single.return_value = {"id": "file::some/file.ts"}
    mock_session.run.return_value = mock_result
    mock_session_ctx.return_value.__aenter__.return_value = mock_session

    tid, pid, conf = await resolve_import("./Button", "some/index.ts", "/workspace")
    assert conf == "EXTRACTED"
    assert tid == "file::some/Button.ts"


@pytest.mark.asyncio
async def test_resolve_builtin():
    tid, pid, conf = await resolve_import("fs", "some/file.ts", "/workspace")
    assert conf == "SKIP"


@pytest.mark.asyncio
@patch("axon.workers.ts.resolver.async_session")
async def test_resolve_package(mock_session_ctx):
    mock_session = AsyncMock()
    mock_result = AsyncMock()
    mock_result.single.return_value = {
        "pkg_id": "pkg1",
        "is_internal": True,
        "bazel_label": "//some:pkg",
    }
    mock_session.run.return_value = mock_result
    mock_session_ctx.return_value.__aenter__.return_value = mock_session

    tid, pid, conf = await resolve_import("@company/ui", "some/file.ts", "/workspace")
    assert conf == "EXTRACTED"
    assert tid == "target:://some:pkg"
    assert pid == "pkg1"
