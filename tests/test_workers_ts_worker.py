from unittest.mock import MagicMock, patch

from axon.workers.ts.worker import TSWorker


@patch("axon.workers.ts.worker.sync_session")
@patch("axon.workers.ts.worker.writers")
@patch("axon.workers.ts.worker.TSWorker._find_ts_files")
@patch("axon.workers.ts.worker.resolve_import")
def test_ts_worker_run(mock_resolve, mock_find, mock_writers, mock_sync_session):
    mock_session = MagicMock()
    mock_sync_session.return_value.__enter__.return_value = mock_session
    # Return one mock target
    mock_session.run.return_value.data.return_value = [
        {"label": "//frontend:app", "package": "frontend"}
    ]
    mock_find.return_value = ["tests/fixtures/CheckoutForm.tsx"]

    # Mock resolve_import which is async
    import asyncio

    future = asyncio.Future()
    future.set_result(("target:://some:pkg", None, "EXTRACTED"))
    mock_resolve.return_value = future

    worker = TSWorker(workspace_root=".")
    result = worker.run()

    assert result.success is True
    assert mock_writers.write_source_files.called
    assert mock_writers.write_symbols.called
