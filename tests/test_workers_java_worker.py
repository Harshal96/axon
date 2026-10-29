from __future__ import annotations

from unittest.mock import MagicMock, patch

from axon.workers.java.worker import JavaWorker


@patch("axon.workers.java.worker.sync_session")
@patch("axon.workers.java.worker.JavaWorker._find_java_files")
@patch("axon.workers.java.worker.writers.write_symbols")
@patch("axon.workers.java.worker.writers.write_defines_edges")
def test_java_worker_run(mock_defines, mock_symbols, mock_find, mock_session_ctx):
    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.data.return_value = [
        {"label": "//services/payment:java", "package": "services/payment", "build_file": "BUILD"}
    ]
    mock_session.run.return_value = mock_result
    mock_session_ctx.return_value.__enter__.return_value = mock_session

    mock_find.return_value = ["tests/fixtures/PaymentGateway.java"]

    worker = JavaWorker(workspace_root=".", commit_sha="abc")

    with patch("axon.workers.java.worker.JavaWorker._fetch_genrule_java_targets", return_value=[]):
        res = worker.run()
        assert res.success is True
        mock_symbols.assert_called()
        mock_defines.assert_called()
