"""Read-only, source-evidenced endpoint regression contracts."""

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from axon.v1.graph import GraphSnapshot
from axon.v1.models import GraphNode, NodeKind
from axon.v1.servicemap import load_service_map


def _matches(node: GraphNode, selector: dict[str, Any]) -> bool:
    return all(
        value == (node.service_id if key == "service" else node.props.get(key, ""))
        for key, value in selector.items()
        if key in {"service", "method", "path", "url"}
    )


def verify_golden(snap: GraphSnapshot, root: Path, path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or data.get("version") != 1 or not data.get("cases"):
        raise ValueError("golden file requires version: 1 and nonempty cases")
    repo_id = load_service_map(root).repo
    results = []
    for case in data["cases"]:
        selector = case["api"]
        if not {"service", "method", "path"} <= selector.keys():
            raise ValueError("golden API requires service, method and path")
        endpoints = [
            ep
            for ep in snap.by_kind(NodeKind.API_ENDPOINT)
            if ep.repo_id == repo_id and _matches(ep, selector)
        ]
        errors = []
        snippets = []
        if len(endpoints) != 1:
            errors.append(f"expected one API, found {len(endpoints)}")
        calls = snap.outbound_calls(endpoints[0].id) if len(endpoints) == 1 else []
        if len(endpoints) == 1:
            handler = snap.handler_of(endpoints[0].id)
            if handler and handler.file and handler.line_start:
                source = (root / handler.file).resolve()
                if not source.is_relative_to(root.resolve()):
                    raise ValueError("golden handler must be inside the repository")
                try:
                    lines = source.read_text().splitlines()
                    snippets.append(
                        f"{handler.file}:{handler.line_start}: {lines[handler.line_start - 1]}"
                    )
                except (OSError, IndexError) as exc:
                    errors.append(f"cannot read handler evidence: {exc}")

        for kind in ("expect", "forbid"):
            for target in case.get(kind, []):
                if not any(k in target for k in ("service", "path", "url")):
                    raise ValueError("golden target requires service, path or url")
                evidence = target.get("evidence", {})
                hits = [
                    (edge, node)
                    for edge, node in calls
                    if _matches(node, target)
                    and (
                        not evidence
                        or (
                            edge.evidence.file == evidence["file"]
                            and edge.evidence.line == evidence["line"]
                        )
                    )
                ]
                if kind == "expect" and not hits:
                    errors.append(f"missing expected target: {target}")
                if kind == "forbid" and hits:
                    errors.append(f"forbidden target present: {target}")
                locations = [(e.evidence.file, e.evidence.line) for e, _ in hits]
                if evidence:
                    locations.append((evidence["file"], evidence["line"]))
                for file, line in dict.fromkeys(locations):
                    source = (root / file).resolve()
                    if not source.is_relative_to(root.resolve()):
                        raise ValueError("golden evidence must be inside the repository")
                    try:
                        lines = source.read_text().splitlines()
                        if not 1 <= line <= len(lines):
                            raise ValueError("line outside source")
                        snippets.append(f"{file}:{line}: {lines[line - 1]}")
                    except (OSError, ValueError) as exc:
                        errors.append(f"cannot read evidence {file}:{line}: {exc}")
        if not case.get("expect") and not case.get("forbid"):
            raise ValueError("golden case requires expect or forbid")
        results.append(
            {
                "id": case.get("id", str(selector)),
                "ok": not errors,
                "errors": errors,
                "snippets": list(dict.fromkeys(snippets)),
            }
        )
    return {"ok": all(r["ok"] for r in results), "cases": results}
