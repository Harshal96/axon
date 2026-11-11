"""LLM provider chain: primary first, fallback on transport/5xx only."""

from types import SimpleNamespace

import httpx
import pytest

from axon.v1.llm import LlmClient, client_from_settings


def settings(**overrides):
    base = {
        "llm_provider": "local",
        "llm_base_url": "http://primary:8000",
        "llm_model": "rlcd-primary",
        "llm_api_key": "k1",
        "llm_timeout_seconds": 5.0,
        "llm_fallback_base_url": "",
        "llm_fallback_model": "",
        "llm_fallback_api_key": "",
        "llm_allowed_models": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_client_from_settings_primary_only():
    client = client_from_settings(settings())
    assert client is not None
    assert [e.model for e in client.endpoints] == ["rlcd-primary"]


def test_client_from_settings_chain_order():
    client = client_from_settings(
        settings(llm_fallback_base_url="http://fallback:8000", llm_fallback_model="gen-fallback")
    )
    assert client is not None
    assert [e.model for e in client.endpoints] == ["rlcd-primary", "gen-fallback"]
    assert client.endpoints[1].base_url == "http://fallback:8000"


def test_client_from_settings_allowlist_filters():
    client = client_from_settings(
        settings(
            llm_allowed_models="gen-fallback",
            llm_fallback_base_url="http://fallback:8000",
            llm_fallback_model="gen-fallback",
        )
    )
    assert client is not None
    assert [e.model for e in client.endpoints] == ["gen-fallback"]
    assert client_from_settings(settings(llm_allowed_models="other-model")) is None


def test_client_from_settings_none_provider():
    assert client_from_settings(settings(llm_provider="none")) is None
    assert client_from_settings(settings(llm_base_url="")) is None


def _client(handler) -> LlmClient:
    return LlmClient(
        endpoints=[
            ("http://primary:8000", "m1", ""),
            ("http://fallback:8000", "m2", ""),
        ],
        transport=httpx.MockTransport(handler),
    )


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": f"from {request.url.host}"}}]}
    )


async def test_chat_falls_back_on_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary":
            return httpx.Response(503, text="down")
        return _ok(request)

    client = _client(handler)
    assert await client.chat("s", "u") == "from fallback"
    assert client.used_model == "m2"


async def test_chat_falls_back_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary":
            raise httpx.ConnectError("refused", request=request)
        return _ok(request)

    assert await _client(handler).chat("s", "u") == "from fallback"


async def test_chat_does_not_fall_back_on_4xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).chat("s", "u")


async def test_chat_raises_when_all_endpoints_fail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).chat("s", "u")
