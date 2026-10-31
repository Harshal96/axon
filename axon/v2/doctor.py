"""Static setup checks; target code and build files are never executed."""

import ast
import re
from pathlib import Path
from typing import Any

from axon.v2.indexer import _is_spec, collect_files
from axon.v2.servicemap import _matches, load_service_map, owner_of
from axon.workers.swagger.parser import parse_swagger_file


def doctor(root: Path) -> dict[str, Any]:
    root = root.resolve()
    sm = load_service_map(root)
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
    dependencies = "\n".join(build_text)
    service_ids = {s.id for s in sm.services}
    spec_owners: dict[str, list[str]] = {}
    for service in sm.services:
        for pattern in service.roots:
            if not list(root.glob(pattern)):
                issue("missing-root", f"{service.id}: {pattern}")
            if any(_matches(pattern.rstrip("/"), shared) for shared in sm.shared):
                issue("root-inside-shared", f"{service.id}: {pattern}")
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
    return {
        "ok": not any(i["severity"] == "error" for i in issues),
        "issues": issues,
        "suggested_ignores": ["**/migrations/**", "**/.venv/**", "**/node_modules/**"],
    }
