import hashlib
import json
import logging
from typing import Any

import httpx

from axon_mcp.cache import (
    cache_epoch,
    get_cache,
    get_cache_ignore_expiry,
    observe_publication,
    set_cache,
)
from axon_mcp.config import load_config

logger = logging.getLogger(__name__)

SUPPORTED_GRAPH_SCHEMA = 2
_schema_checked: set[str] = set()


def _server_identity(config) -> str:
    # Include auth scope without storing credentials in keys or logs.
    return hashlib.sha256(
        json.dumps([config.api_url.rstrip("/"), config.token]).encode()
    ).hexdigest()


def _publication(response, data) -> str:
    value = response.headers.get("X-Axon-Publication-Id", "")
    if isinstance(value, str) and value:
        return value
    value = data.get("publication_id", "") if isinstance(data, dict) else ""
    return value if isinstance(value, str) else ""


async def _warn_on_schema_skew(config, namespace: str) -> None:
    if namespace in _schema_checked:
        return
    _schema_checked.add(namespace)
    epoch = cache_epoch(namespace)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{config.api_url.rstrip('/')}/api/v1/health")
            resp.raise_for_status()
            data = resp.json()
            server_schema = data.get("graph_schema_version")
            observe_publication(namespace, _publication(resp, data), expected_epoch=epoch)
        if server_schema is not None and int(server_schema) != SUPPORTED_GRAPH_SCHEMA:
            logger.warning(
                "Axon server graph schema %s differs from MCP supported %s; "
                "upgrade the older side before relying on results",
                server_schema,
                SUPPORTED_GRAPH_SCHEMA,
            )
    except Exception:
        pass


async def proxy_call(
    method: str,
    path: str,
    params: dict | None = None,
    json_body: dict | None = None,
    cache_ttl: int = 300,
) -> Any:
    config = load_config()
    namespace = _server_identity(config)
    await _warn_on_schema_skew(config, namespace)
    epoch = cache_epoch(namespace)

    # Generate cache key
    cache_key = json.dumps([method.upper(), path, params, json_body], sort_keys=True)

    if method.upper() == "GET":
        cached = get_cache(cache_key, namespace=namespace)
        if cached is not None:
            return cached

    url = f"{config.api_url.rstrip('/')}{path}"
    headers = {}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"

    try:
        async with httpx.AsyncClient() as client:
            if method.upper() == "GET":
                response = await client.get(url, params=params, headers=headers)
            elif method.upper() == "POST":
                response = await client.post(url, json=json_body, headers=headers)
            else:
                raise ValueError(f"Unsupported method {method}")

            response.raise_for_status()
            data = response.json()

            observed_epoch, accepted = observe_publication(
                namespace, _publication(response, data), expected_epoch=epoch
            )
            if method.upper() == "GET" and accepted:
                set_cache(
                    cache_key, data, cache_ttl, namespace=namespace, expected_epoch=observed_epoch
                )

            return data

    except httpx.HTTPStatusError as e:
        logger.error(f"API Error {e.response.status_code}: {e.response.text}")
        try:
            details = e.response.json()
        except Exception:
            details = e.response.text

        # A missing endpoint can reveal a committed target removal. Observe
        # its snapshot too, while fencing errors from an older in-flight epoch.
        observe_publication(namespace, _publication(e.response, details), expected_epoch=epoch)
        return {
            "error": True,
            "status_code": e.response.status_code,
            "message": "API Error",
            "details": details,
        }
    except httpx.RequestError as e:
        logger.error(f"Unreachable API: {e}")
        # Degraded fallback logic: returning cached data if available
        if method.upper() == "GET":
            cached = get_cache_ignore_expiry(cache_key, namespace=namespace)
            if cached is not None:
                if not isinstance(cached, dict):
                    logger.warning(
                        "STALE_FALLBACK: Service unreachable, showing stale cached list."
                    )
                    return cached
                warnings = cached.get("warnings", [])
                warnings.append(
                    {
                        "code": "STALE_FALLBACK",
                        "message": "Service unreachable, showing stale cached data.",
                    }
                )
                cached["warnings"] = warnings
                return cached

        return {"error": True, "message": "Service unreachable", "details": str(e)}
