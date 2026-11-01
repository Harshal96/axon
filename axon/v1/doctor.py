"""Static setup checks; target code and build files are never executed."""

import ast
import re
from pathlib import Path
from typing import Any

from axon.v1.indexer import _is_spec, collect_files
from axon.v1.servicemap import ServiceMap, load_service_map, owner_of
from axon.v1.swagger import parse_swagger_file


def doctor(root: Path, *, service_map: ServiceMap | None = None) -> dict[str, Any]:
    root = root.resolve()
    sm = service_map or load_service_map(root)
    issues: list[dict[str, str]] = []

    def issue(code: str, message: str, severity: str = "error") -> None:
        issues.append({"code": code, "message": message, "severity": severity})

    imports: set[str] = set()
    build_text = []
    files = collect_files(root, sm)
    for rel in files:
        path = root / rel
        if path.name in {"BUILD", "BUILD.bazel"}:
            build_text.append(path.read_text(errors="replace"))
        elif path.suffix == ".py":
            try:
                tree = ast.parse(path.read_text(errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module)
                    imports.update(node.module + "." + alias.name for alias in node.names)
        elif path.suffix in {".java", ".go"}:
            imports.update(
                re.findall(
                    r'(?:import\s+|import\s*\(\s*)["\s]*([\w./]+)', path.read_text(errors="replace")
                )
            )
    from axon.v1.client_discovery import discover_clients

    enriched = discover_clients(root, sm, files)
    for service, discovered in zip(sm.services, enriched.services, strict=True):
        for client, located in zip(service.clients, discovered.clients, strict=False):
            if not client.paths and located.paths:
                issue(
                    "client-source-paths",
                    f"{service.id}/{client.id}: suggested paths: {located.paths}",
                    "warning",
                )
    dependencies = "\n".join(build_text)
    service_ids = {s.id for s in sm.services}
    spec_owners: dict[str, list[str]] = {}
    for service in sm.services:
        for pattern in service.roots:
            if not list(root.glob(pattern)):
                issue("missing-root", f"{service.id}: {pattern}")
            if any(pattern.rstrip("/") == shared.rstrip("/") for shared in sm.shared):
                issue(
                    "root-inside-shared",
                    f"{service.id}: {pattern} is also declared shared; "
                    "service wins equal-root ties",
                    "warning",
                )
        for spec in service.specs:
            spec_owners.setdefault(spec, []).append(service.id)
        for client in service.clients:
            if (
                client.target_service
                and not client.target_repo
                and client.target_service not in service_ids
            ):
                issue("unknown-target", f"{service.id}/{client.id}: {client.target_service}")
            used = any(
                any(name == package or name.startswith(package + ".") for name in imports)
                or package in dependencies
                for package in client.packages
            )
            if client.packages and not used:
                issue(
                    "unused-client",
                    f"{service.id}/{client.id}: no import or BUILD dependency",
                    "warning",
                )
    for rel in files:
        if _is_spec(rel) and rel not in spec_owners:
            kind, service_id = owner_of(sm, rel)
            spec_owners[rel] = [service_id] if kind == "service" else []
    for service in sm.services:
        for client in service.clients:
            if client.spec and "::" not in client.spec and client.spec not in spec_owners:
                spec_owners[client.spec] = []
    for spec, owners in spec_owners.items():
        if len(owners) != 1:
            issue("spec-owners", f"{spec}: expected one owner, found {owners}")
        if not (root / spec).is_file():
            issue("missing-spec", spec)
        else:
            try:
                if not parse_swagger_file(spec, str(root)):
                    issue("invalid-spec", spec)
            except Exception as exc:
                issue("invalid-spec", f"{spec}: {exc}")
    from axon.v1.client_discovery import suggest_base_urls
    from axon.v1.onboard import hidden_service_notes
    from axon.v1.onboard_suggestions import path_base_url_suggestions

    for note in hidden_service_notes(root, sm, files):
        issue("hidden-service", note, "warning")
    for service in sm.services:
        for call in service.calls:
            path = (root / call.evidence.file).resolve()
            try:
                path.relative_to(root)
                lines = path.read_text(errors="replace").splitlines()
                valid = (
                    call.evidence.line <= len(lines)
                    and call.evidence.text in lines[call.evidence.line - 1]
                )
            except (OSError, ValueError):
                valid = False
            if not valid:
                issue(
                    "stale-call-evidence",
                    f"{service.id}/{call.id}: {call.evidence.file}:{call.evidence.line}",
                    "warning",
                )
    # Evaluate graph-sensitive stale checks only when corrections are present.
    if service_map is None and (
        sm.links or any(s.links or s.endpoints or s.suppress or s.handlers for s in sm.services)
    ):
        from axon.v1.indexer import index_repo

        snap, _ = index_repo(root, workers=1)
        rules = {e.evidence.rule for e in snap.edges}
        from axon.v1.manual_corrections import manual_sources, provenance

        sources = manual_sources(root)
        for scope, link in [("", link) for link in sm.links] + [
            (s.id, link) for s in sm.services for link in s.links
        ]:
            rule, _ = provenance(sources, f"link:{scope + '/' if scope else ''}{link.id}", link.id)
            if not any(r == rule or (not link.id and r == rule.rsplit("#", 1)[0]) for r in rules):
                issue(
                    "unmatched-link",
                    f"{scope}/{link.id or link.call_name}: matches zero indexed calls",
                    "warning",
                )
        for diagnostic in snap.diagnostics:
            code = diagnostic.split(":", 1)[0].split("#", 1)[0]
            if code in {
                "unmatched-suppress",
                "redundant-endpoint",
                "unmatched-handler",
                "unresolved-handler",
                "unmatched-call",
            }:
                issue(code, diagnostic, "warning")
    return {
        "base_url_suggestions": [
            *suggest_base_urls(root, sm, files),
            *path_base_url_suggestions(root, sm, files),
        ],
        "ok": not any(i["severity"] == "error" for i in issues),
        "issues": issues,
        "suggested_ignores": ["**/migrations/**", "**/.venv/**", "**/node_modules/**"],
    }
