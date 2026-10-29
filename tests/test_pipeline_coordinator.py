from unittest.mock import AsyncMock, patch

import pytest

from axon.pipeline.coordinator import run_pipeline_async


@pytest.mark.asyncio
@patch("axon.pipeline.coordinator.is_commit_indexed", new_callable=AsyncMock)
@patch("axon.pipeline.coordinator.write_commit_snapshot", new_callable=AsyncMock)
@patch("axon.pipeline.coordinator.invalidate_redis_cache")
@patch("axon.pipeline.coordinator.broadcast_graph_updated")
async def test_coordinator_skips_indexed_commit(
    mock_broadcast, mock_invalidate, mock_write, mock_is_indexed
):
    mock_is_indexed.return_value = True
    await run_pipeline_async("sha123", ["BUILD"])

    mock_write.assert_not_called()
    mock_invalidate.assert_not_called()


@pytest.mark.asyncio
@patch("axon.pipeline.coordinator.is_commit_indexed", new_callable=AsyncMock)
@patch("axon.pipeline.coordinator.write_commit_snapshot", new_callable=AsyncMock)
@patch("axon.pipeline.coordinator.invalidate_redis_cache")
@patch("axon.pipeline.coordinator.broadcast_graph_updated")
async def test_coordinator_runs_pipeline(
    mock_broadcast, mock_invalidate, mock_write, mock_is_indexed
):
    mock_is_indexed.return_value = False

    await run_pipeline_async("sha123", ["BUILD"])

    # Assert writing the commit snapshot
    mock_write.assert_called_once()
    args, kwargs = mock_write.call_args
    assert args[0] == "sha123"
    assert args[1] is True  # success

    mock_invalidate.assert_called_once()
    mock_broadcast.assert_called_once_with("sha123")
