from unittest.mock import MagicMock, patch

import pytest
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

    mock_httpx_get.side_effect = httpx.HTTPStatusError(
        "Error", request=MagicMock(), response=mock_response
    )

    result = await proxy_call("GET", "/api/test")
    assert result["error"] is True
    assert result["status_code"] == 404


@pytest.mark.asyncio
@patch("axon_mcp.tools._base.httpx.AsyncClient.get")
@patch("axon_mcp.tools._base.get_cache")
@patch("axon_mcp.tools._base.get_cache_ignore_expiry")
async def test_proxy_call_unreachable_fallback(
    mock_get_cache_ignore, mock_get_cache, mock_httpx_get
):
    import httpx

    mock_get_cache.return_value = None
    mock_get_cache_ignore.return_value = {"old_data": True}

    mock_httpx_get.side_effect = httpx.RequestError("Unreachable")

    result = await proxy_call("GET", "/api/test")
    assert result["old_data"] is True
    assert "STALE_FALLBACK" in result["warnings"][0]["code"]


@pytest.mark.asyncio
async def test_observed_generation_invalidates_lists_and_preserves_qualified_evidence(monkeypatch):
    import httpx
    from axon_mcp.config import Config
    from axon_mcp.tools import _base

    generation = "one"
    requests = []
    evidence = {
        "id": "endpoint::B::ledger::GET:/entries",
        "repo_id": "B",
        "service_id": "ledger",
        "evidence": {"rule": "manual", "file": "app.py", "line": 7},
    }

    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith("health"):
            return httpx.Response(
                200, json={"graph_schema_version": 2, "publication_id": generation}
            )
        if request.url.path.endswith("explain"):
            return httpx.Response(
                200,
                json={
                    "reason": "target-repo-missing",
                    "target_repo": "B",
                    "publication_id": generation,
                    "candidate_nodes": [],
                },
                headers={"X-Axon-Publication-Id": generation},
            )
        return httpx.Response(
            200,
            json=[{**evidence, "generation": generation}],
            headers={"X-Axon-Publication-Id": generation},
        )

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        _base.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(_base, "load_config", lambda: Config(api_url="http://one.test"))
    first = await proxy_call("GET", "/api/v1/apis")
    assert first[0]["id"] == evidence["id"] and first[0]["evidence"] == evidence["evidence"]
    generation = "two"
    assert (await proxy_call("GET", "/api/v1/apis"))[0][
        "generation"
    ] == "one"  # TTL between observations
    assert (await proxy_call("GET", "/explain"))["reason"] == "target-repo-missing"
    assert (await proxy_call("GET", "/api/v1/apis"))[0]["generation"] == "two"
    assert requests.count("/api/v1/apis") == 2
    monkeypatch.setattr(_base, "load_config", lambda: Config(api_url="http://two.test"))
    generation = "other"
    assert (await proxy_call("GET", "/api/v1/apis"))[0]["generation"] == "other"


@pytest.mark.asyncio
@pytest.mark.parametrize("old_status", [200, 404])
async def test_overlapping_old_response_does_not_roll_back_observation_or_refill(
    monkeypatch, old_status
):
    import asyncio

    import httpx
    from axon_mcp.config import Config
    from axon_mcp.tools import _base

    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def respond(request):
        nonlocal count
        if request.url.path.endswith("health"):
            return httpx.Response(200, json={"graph_schema_version": 2, "publication_id": "one"})
        if request.url.path == "/slow":
            count += 1
            if count == 1:
                started.set()
                await release.wait()
                return httpx.Response(
                    old_status,
                    json={"publication_id": "one", "old": True},
                    headers={"X-Axon-Publication-Id": "one"},
                )
        return httpx.Response(200, json={"publication_id": "two", "old": False})

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        _base.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(_base, "load_config", lambda: Config(api_url="http://race.test"))
    old = asyncio.create_task(proxy_call("GET", "/slow"))
    await started.wait()
    assert not (await proxy_call("GET", "/new"))["old"]
    release.set()
    old_result = await old  # The original caller still receives its response.
    assert (old_result["details"] if old_status == 404 else old_result)["old"]
    assert not (await proxy_call("GET", "/slow"))["old"]
    assert count == 2


@pytest.mark.asyncio
async def test_legacy_server_empty_lists_cache_and_empty_ids_keep_observed_entries(monkeypatch):
    import httpx
    from axon_mcp.config import Config
    from axon_mcp.tools import _base

    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("health"):
            return httpx.Response(200, json={"graph_schema_version": 2})
        if request.url.path == "/generation":
            return httpx.Response(200, json={"publication_id": "known"})
        return httpx.Response(200, json=[])

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        _base.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(_base, "load_config", lambda: Config(api_url="http://legacy.test"))
    assert await proxy_call("GET", "/empty") == []
    assert await proxy_call("GET", "/empty") == []
    assert calls.count("/empty") == 1
    await proxy_call("GET", "/generation")
    assert await proxy_call("GET", "/empty") == []  # first known generation invalidated legacy data
    assert calls.count("/empty") == 2
    await proxy_call("GET", "/no-id")
    assert await proxy_call("GET", "/empty") == []
    assert calls.count("/empty") == 2


@pytest.mark.asyncio
async def test_unreachable_list_fallback_keeps_array_shape(monkeypatch, caplog):
    import httpx
    from axon_mcp.config import Config
    from axon_mcp.tools import _base

    reachable = True

    def respond(request):
        if not reachable:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json=[])

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        _base.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(_base, "load_config", lambda: Config(api_url="http://offline.test"))
    assert await proxy_call("GET", "/apis", cache_ttl=-1) == []
    reachable = False
    assert await proxy_call("GET", "/apis") == []
    assert "STALE_FALLBACK" in caplog.text


@pytest.mark.asyncio
async def test_removed_target_error_header_invalidates_stale_generation(monkeypatch):
    import httpx
    from axon_mcp.config import Config
    from axon_mcp.tools import _base

    generation = "one"

    def respond(request):
        if request.url.path.endswith("health"):
            return httpx.Response(
                200, json={"graph_schema_version": 2, "publication_id": generation}
            )
        if request.url.path == "/removed":
            return httpx.Response(
                404,
                json={"detail": "endpoint not found"},
                headers={"X-Axon-Publication-Id": generation},
            )
        return httpx.Response(200, json=[generation], headers={"X-Axon-Publication-Id": generation})

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        _base.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(_base, "load_config", lambda: Config(api_url="http://removed.test"))
    assert await proxy_call("GET", "/apis") == ["one"]
    generation = "two"
    assert (await proxy_call("GET", "/removed"))["status_code"] == 404
    assert await proxy_call("GET", "/apis") == ["two"]
