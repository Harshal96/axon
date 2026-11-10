import logging
import httpx
from typing import Any, Dict, Optional
from axon_mcp.config import load_config
from axon_mcp.cache import get_cache, set_cache, get_cache_ignore_expiry

logger = logging.getLogger(__name__)

SUPPORTED_GRAPH_SCHEMA = 1
_schema_checked = False


async def _warn_on_schema_skew(config) -> None:
    global _schema_checked
    if _schema_checked:
        return
    _schema_checked = True
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{config.api_url}/api/v1/health")
            server_schema = resp.json().get("graph_schema_version")
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
    params: Optional[dict] = None, 
    json_body: Optional[dict] = None,
    cache_ttl: int = 300
) -> Dict[str, Any]:
    config = load_config()
    await _warn_on_schema_skew(config)

    # Generate cache key
    cache_key = f"{method}:{path}:{params}:{json_body}"
    
    if method.upper() == "GET":
        cached = get_cache(cache_key)
        if cached:
            return cached
            
    url = f"{config.api_url}{path}"
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
            
            if method.upper() == "GET":
                set_cache(cache_key, data, cache_ttl)
                
            return data
            
    except httpx.HTTPStatusError as e:
        logger.error(f"API Error {e.response.status_code}: {e.response.text}")
        try:
            details = e.response.json()
        except Exception:
            details = e.response.text
            
        return {
            "error": True, 
            "status_code": e.response.status_code, 
            "message": "API Error", 
            "details": details
        }
    except httpx.RequestError as e:
        logger.error(f"Unreachable API: {e}")
        # Degraded fallback logic: returning cached data if available
        if method.upper() == "GET":
            cached = get_cache_ignore_expiry(cache_key)
            if cached:
                warnings = cached.get("warnings", [])
                warnings.append({"code": "STALE_FALLBACK", "message": "Service unreachable, showing stale cached data."})
                cached["warnings"] = warnings
                return cached
                
        return {
            "error": True,
            "message": "Service unreachable",
            "details": str(e)
        }
