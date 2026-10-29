import pytest
from unittest.mock import patch, MagicMock
from axon_mcp.tools._base import proxy_call

@pytest.mark.asyncio
@patch("axon_mcp.tools._base.httpx.AsyncClient.get")
@patch("axon_mcp.tools._base.get_cache")
@patch("axon_mcp.tools._base.set_cache")
async def test_proxy_call_happy_path(mock_set_cache, mock_get_cache, mock_httpx_get):
    mock_get_cache.return_value = None
    mock_response = MagicMock()
    mock_response.json.return_value = {"success": True}
    mock_httpx_get.return_value = mock_response

    result = await proxy_call("GET", "/api/test")
    assert result == {"success": True}
    mock_set_cache.assert_called_once()

@pytest.mark.asyncio
@patch("axon_mcp.tools._base.httpx.AsyncClient.get")
@patch("axon_mcp.tools._base.get_cache")
async def test_proxy_call_api_error(mock_get_cache, mock_httpx_get):
    import httpx
    mock_get_cache.return_value = None
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not Found"
    mock_response.json.side_effect = Exception("No JSON")
    
    mock_httpx_get.side_effect = httpx.HTTPStatusError("Error", request=MagicMock(), response=mock_response)

    result = await proxy_call("GET", "/api/test")
    assert result["error"] is True
    assert result["status_code"] == 404

@pytest.mark.asyncio
@patch("axon_mcp.tools._base.httpx.AsyncClient.get")
@patch("axon_mcp.tools._base.get_cache")
@patch("axon_mcp.tools._base.get_cache_ignore_expiry")
async def test_proxy_call_unreachable_fallback(mock_get_cache_ignore, mock_get_cache, mock_httpx_get):
    import httpx
    mock_get_cache.return_value = None
    mock_get_cache_ignore.return_value = {"old_data": True}
    
    mock_httpx_get.side_effect = httpx.RequestError("Unreachable")

    result = await proxy_call("GET", "/api/test")
    assert result["old_data"] is True
    assert "STALE_FALLBACK" in result["warnings"][0]["code"]
