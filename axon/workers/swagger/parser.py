"""
Parses swagger.json / openapi.yaml files into structured objects.
Supports both OpenAPI 3.x and Swagger 2.0.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ParsedEndpoint:
    """A single HTTP endpoint extracted from a spec."""

    path: str  # "/v1/charges/{id}"
    method: str  # "POST"
    operation_id: str | None = None
    summary: str | None = None
    deprecated: bool = False
    schema_hash: str = ""  # hash of request+response schema


@dataclass
class ParsedSwagger:
    """Complete parse result for one swagger/openapi spec."""

    path: str  # repo-relative path
    openapi_version: str
    content_hash: str
    title: str = ""
    base_path: str = ""
    endpoints: list[ParsedEndpoint] = field(default_factory=list)


def _hash_operation(op: dict) -> str:
    """Deterministic hash of an operation's schema for change detection."""
    canonical = json.dumps(op, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def parse_swagger_file(file_path: str, repo_root: str) -> ParsedSwagger | None:
    """
    Parse a swagger.json or openapi.yaml file.
    Returns None if file doesn't exist or can't be parsed.
    """
    abs_path = Path(repo_root) / file_path
    if not abs_path.exists():
        logger.warning("Swagger file not found: %s", file_path)
        return None

    try:
        raw = abs_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Could not read %s: %s", file_path, e)
        return None

    content_hash = hashlib.sha256(raw.encode()).hexdigest()

    try:
        if file_path.endswith((".yaml", ".yml")):
            spec = yaml.safe_load(raw)
        else:
            spec = json.loads(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        logger.warning("Could not parse %s: %s", file_path, e)
        return None

    if not isinstance(spec, dict):
        return None

    return parse_swagger_dict(spec, file_path, content_hash)


def parse_swagger_dict(
    spec: dict,
    file_path: str,
    content_hash: str,
) -> ParsedSwagger:
    """Parse an already-loaded swagger/openapi dict."""
    # Detect version
    openapi_version = spec.get("openapi") or spec.get("swagger") or "unknown"
    title = ""
    info = spec.get("info")
    if isinstance(info, dict):
        title = info.get("title", "")

    base_path = str(spec.get("basePath") or "")
    if not base_path:
        servers = spec.get("servers") or []
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            base_path = urlsplit(str(servers[0].get("url") or "")).path
    endpoints: list[ParsedEndpoint] = []
    paths = spec.get("paths", {})

    if not isinstance(paths, dict):
        paths = {}

    for path_str, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            endpoints.append(
                ParsedEndpoint(
                    path=(base_path.rstrip("/") + "/" + str(path_str).lstrip("/")),
                    method=method.upper(),
                    operation_id=op.get("operationId"),
                    summary=op.get("summary"),
                    deprecated=bool(op.get("deprecated", False)),
                    schema_hash=_hash_operation(op),
                )
            )

    return ParsedSwagger(
        path=file_path,
        openapi_version=str(openapi_version),
        content_hash=content_hash,
        title=title,
        base_path=base_path,
        endpoints=endpoints,
    )
