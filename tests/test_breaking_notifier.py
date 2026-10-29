import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from axon.breaking.notifier import notify_breaking_changes


@pytest.mark.asyncio
@patch("axon.breaking.notifier.get_settings")
@patch("axon.breaking.notifier.httpx.AsyncClient")
async def test_notify_breaking_changes_empty_url(mock_client_cls, mock_settings) -> None:
    mock_settings.return_value.slack_webhook_url = ""
    
    count = await notify_breaking_changes([{"id": "alert1"}], "abc", "author")
    assert count == 0
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
@patch("axon.breaking.notifier.get_settings")
@patch("axon.breaking.notifier.httpx.AsyncClient")
async def test_notify_breaking_changes_success(mock_client_cls, mock_settings) -> None:
    mock_settings.return_value.slack_webhook_url = "http://mock.slack"
    
    mock_client = AsyncMock()
    mock_client_cls.return_value.__aenter__.return_value = mock_client
    mock_resp = MagicMock()
    mock_client.post.return_value = mock_resp
    
    alerts = [
        {
            "id": "a1", 
            "severity": "CRITICAL", 
            "alert_type": "API_SCHEMA", 
            "node_id": "ep1", 
            "old_value": "old",
            "new_value": "new",
            "affected_teams": ["t1", "t2"]
        }
    ]
    
    count = await notify_breaking_changes(alerts, "abc", "author")
    assert count == 1
    mock_client.post.assert_called_once()
    payload = mock_client.post.call_args[1]["json"]
    assert "t1, t2" in payload["text"]
    assert "CRITICAL" in payload["text"]


@pytest.mark.asyncio
@patch("axon.breaking.notifier.get_settings")
@patch("axon.breaking.notifier.httpx.AsyncClient")
async def test_notify_breaking_changes_failure_handled(mock_client_cls, mock_settings) -> None:
    mock_settings.return_value.slack_webhook_url = "http://mock.slack"
    
    mock_client = AsyncMock()
    mock_client_cls.return_value.__aenter__.return_value = mock_client
    mock_client.post.side_effect = Exception("Slack down")
    
    alerts = [
        {"id": "a1", "severity": "CRITICAL"}
    ]
    
    count = await notify_breaking_changes(alerts, "abc", "author")
    assert count == 0
