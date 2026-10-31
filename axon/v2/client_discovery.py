"""Read generated-client package hints within explicitly owned services.

Build files are parsed as data, never executed or used to discover owners.
Conflicting package hints are left unresolved; axon.yaml declarations win.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from axon.v2.servicemap import ClientDecl, ServiceMap, owner_of

_PACKAGE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z")
_CONFIG_KEYS = {"packageName", "apiPackage", "python_wheel_name", "java_api_package"}


def _packages(name: str, text: str) -> set[str]:
    values: list[object] = []
    if name in {"BUILD", "BUILD.bazel"}:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            rule = node.func.id if isinstance(node.func, ast.Name) else ""
            if "swagger_client" not in rule:
                continue
            values.extend(
                kw.value.value
                for kw in node.keywords
                if kw.arg in _CONFIG_KEYS and isinstance(kw.value, ast.Constant)
            )
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            data = None
        if isinstance(data, dict):
            values.extend(data.get(key) for key in _CONFIG_KEYS)
            extra = data.get("additionalProperties")
            if isinstance(extra, dict):
                values.extend(extra.get(key) for key in _CONFIG_KEYS)
        # Java properties-style generator configuration.
        values.extend(re.findall(r"(?m)^\s*(?:packageName|apiPackage)\s*=\s*([\w.]+)\s*$", text))
    return {v for v in values if isinstance(v, str) and _PACKAGE.fullmatch(v)}


def discover_clients(root: Path, sm: ServiceMap, files: list[str]) -> ServiceMap:
    """Return an enriched copy; never rewrite the committed service map.

    Only literal package metadata from swagger.cfg or a swagger_client build
    rule is accepted, and only beneath a service already declared in the map.
    Generated package directories are marked as client sources to distinguish
    their transport implementations from the service's own outbound calls.
    """
    result = sm.model_copy(deep=True)
    hints: dict[str, set[str]] = defaultdict(set)
    for rel in files:
        name = Path(rel).name
        if name not in {"swagger.cfg", "BUILD", "BUILD.bazel"}:
            continue
        kind, sid = owner_of(sm, rel)
        if kind != "service":
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for package in _packages(name, text):
            hints[package].add(sid)
    explicit = {
        package for svc in sm.services for client in svc.clients for package in client.packages
    }
    by_service: dict[str, list[str]] = defaultdict(list)
    for package, owners in sorted(hints.items()):
        if len(owners) == 1 and package not in explicit:
            by_service[next(iter(owners))].append(package)
    # One pass over source paths, rather than a scan per service/package.
    generated_paths: dict[str, set[str]] = defaultdict(set)
    for rel in files:
        kind, sid = owner_of(sm, rel)
        if kind != "service" or sid not in by_service:
            continue
        parts = Path(rel).parts
        packages = by_service[sid]
        for package in packages:
            segments = tuple(package.split("."))
            for i in range(len(parts) - len(segments)):
                if parts[i : i + len(segments)] == segments:
                    generated_paths[sid].add(Path(*parts[: i + len(segments)]).as_posix())
                    break
    for svc in result.services:
        service_packages = by_service.get(svc.id)
        if not service_packages:
            continue
        cid = f"discovered-{svc.id}"
        used = {c.id for s in result.services for c in s.clients}
        while cid in used:
            cid += "-generated"
        svc.clients.append(
            ClientDecl(
                id=cid,
                packages=service_packages,
                paths=sorted(generated_paths[svc.id]),
                target_service=svc.id,
                spec=svc.specs[0] if len(svc.specs) == 1 else "",
            )
        )
    return result
