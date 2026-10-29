import pytest
from unittest.mock import MagicMock, patch

from axon.breaking.pr_checker import cli_main


@patch("axon.breaking.pr_checker.argparse.ArgumentParser.parse_args")
@patch("axon.breaking.pr_checker.httpx.get")
def test_pr_checker_pass_no_alerts(mock_get, mock_args) -> None:
    mock_args.return_value = MagicMock(
        base_sha="base",
        head_sha="head",
        fail_on="CRITICAL,HIGH",
        dry_run=False
    )
    
    mock_resp1 = MagicMock()
    mock_resp2 = MagicMock()
    mock_resp2.json.return_value = []
    
    mock_get.side_effect = [mock_resp1, mock_resp2]
    
    assert cli_main() == 0


@patch("axon.breaking.pr_checker.argparse.ArgumentParser.parse_args")
@patch("axon.breaking.pr_checker.httpx.get")
def test_pr_checker_fail_on_critical(mock_get, mock_args) -> None:
    mock_args.return_value = MagicMock(
        base_sha="base",
        head_sha="head",
        fail_on="CRITICAL,HIGH",
        dry_run=False
    )
    
    mock_resp1 = MagicMock()
    mock_resp2 = MagicMock()
    mock_resp2.json.return_value = [
        {"severity": "CRITICAL", "alert_type": "API_SCHEMA", "node_id": "ep1"}
    ]
    
    mock_get.side_effect = [mock_resp1, mock_resp2]
    
    assert cli_main() == 1


@patch("axon.breaking.pr_checker.argparse.ArgumentParser.parse_args")
@patch("axon.breaking.pr_checker.httpx.get")
def test_pr_checker_pass_dry_run(mock_get, mock_args) -> None:
    mock_args.return_value = MagicMock(
        base_sha="base",
        head_sha="head",
        fail_on="CRITICAL,HIGH",
        dry_run=True
    )
    
    mock_resp1 = MagicMock()
    mock_resp2 = MagicMock()
    mock_resp2.json.return_value = [
        {"severity": "CRITICAL", "alert_type": "API_SCHEMA", "node_id": "ep1"}
    ]
    
    mock_get.side_effect = [mock_resp1, mock_resp2]
    
    assert cli_main() == 0
