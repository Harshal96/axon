from unittest.mock import MagicMock, patch

from axon.workers.pnpm.worker import PnpmWorker


@patch("axon.workers.pnpm.worker.sync_session")
@patch("axon.workers.pnpm.worker.writers")
@patch("axon.workers.pnpm.workspace_resolver.sync_session")
def test_pnpm_worker_run(mock_ws_session, mock_writers, mock_worker_session):
    mock_session = MagicMock()
    mock_ws_session.return_value.__enter__.return_value = mock_session
    mock_worker_session.return_value.__enter__.return_value = mock_session

    mock_session.run.return_value.data.return_value = []

    worker = PnpmWorker(workspace_root=".", lockfile_paths=["tests/fixtures/pnpm-lock.yaml"])
    result = worker.run()

    assert result.success is True
    assert mock_writers.write_pnpm_packages.called
    assert mock_writers.write_pnpm_dep_edges.called
