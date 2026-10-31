"""Deterministic, read-only sampling of source-evidenced cross-service edges."""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from axon.v2.graph import GraphSnapshot
from axon.v2.models import EdgeKind, GraphEdge, GraphNode, NodeKind

VERDICTS = {"ok", "wrong-handler", "false-edge", "missing-edge"}


def _location(root: Path, file: str, line: int) -> dict[str, Any]:
    snippet = ""
    source = (root / file).resolve()
    if file and source.is_relative_to(root.resolve()) and line > 0:
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
            if line <= len(lines):
                snippet = lines[line - 1]
        except (OSError, UnicodeError):
            pass
    return {"file": file, "line": line, "snippet": snippet}


def _symbol(root: Path, node: GraphNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "name": node.name,
        "line_end": node.line_end,
        **_location(root, node.file, node.line_start),
    }


def _selector(node: GraphNode) -> dict[str, str]:
    return {
        "service": node.service_id,
        "method": node.props.get("method", ""),
        "path": node.props.get("path", ""),
    }


def _paths(snap: GraphSnapshot, endpoint: str) -> dict[str, list[str]]:
    paths = {node.id: [node.id] for node in snap.handlers_of(endpoint)}
    queue = deque(sorted(paths))
    while queue:
        current = queue.popleft()
        if len(paths[current]) > 12:
            continue
        for target in sorted(
            edge.to_id for edge in snap._out().get(current, []) if edge.kind == EdgeKind.CALLS
        ):
            if target not in paths and target in snap.nodes:
                paths[target] = [*paths[current], target]
                queue.append(target)
    return paths


def _row(
    snap: GraphSnapshot,
    root: Path,
    api: GraphNode,
    edge: GraphEdge,
    target: GraphNode,
    chain: list[str],
    gap: int,
) -> dict[str, Any]:
    handler, caller = snap.nodes[chain[0]], snap.nodes[chain[-1]]
    flags = []
    if handler.line_start and handler.line_end < handler.line_start:
        flags.append("handler-outside-symbol-range")
    handles = next(
        (
            e
            for e in snap.edges
            if e.kind == EdgeKind.HANDLES and e.from_id == handler.id and e.to_id == api.id
        ),
        None,
    )
    route_line = handles.evidence.line if handles else api.line_start
    route_file = handles.evidence.file if handles else api.file
    if route_file == handler.file and route_line and handler.line_start - route_line > gap:
        flags.append("route-handler-gap")
    if (
        edge.evidence.file != caller.file
        or not caller.line_start <= edge.evidence.line <= caller.line_end
    ):
        flags.append("call-site-outside-caller")
    row = {
        "api": _selector(api),
        "handler": _symbol(root, handler),
        "chain": [_symbol(root, snap.nodes[sid]) for sid in chain],
        "call_site": _location(root, edge.evidence.file, edge.evidence.line),
        "target": _selector(target),
        "target_location": _location(root, target.file, target.line_start),
        "rule": edge.evidence.rule,
        "confidence": edge.confidence.value,
        "flags": flags,
        "verdict": "",
    }
    # Review identity binds all displayed source evidence, including handler and
    # intermediate symbols. An old verdict cannot approve a changed chain.
    import json

    identity = json.dumps({"endpoint_id": api.id, **row}, sort_keys=True)
    row["id"] = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return row


def sample_edges(
    snap: GraphSnapshot,
    root: Path,
    *,
    services: int = 5,
    apis: int = 5,
    seed: int = 0,
    route_gap: int = 12,
    repo_id: str = "",
) -> dict[str, Any]:
    """Sample up to ``apis`` eligible endpoints per sampled service.

    All outbound evidence rows for each selected API are retained. No target
    repository code is imported or executed.
    """
    if services < 1 or apis < 1 or route_gap < 0:
        raise ValueError("services/apis must be positive and route gap nonnegative")
    candidates: dict[str, list[tuple[GraphNode, list[tuple[GraphEdge, GraphNode]]]]] = defaultdict(
        list
    )
    for api in snap.by_kind(NodeKind.API_ENDPOINT):
        if repo_id and api.repo_id != repo_id:
            continue
        outbound = [
            (e, n)
            for e, n in snap.outbound_calls(api.id)
            if n.kind == NodeKind.API_ENDPOINT and n.service_id != api.service_id
        ]
        if outbound:
            candidates[api.service_id].append((api, outbound))
    rng = random.Random(seed)
    chosen = rng.sample(sorted(candidates), min(services, len(candidates)))
    rows = []
    for service in chosen:
        endpoints = candidates[service]
        for api, outbound in rng.sample(endpoints, min(apis, len(endpoints))):
            paths = _paths(snap, api.id)
            outbound = [
                (edge, snap.nodes[edge.to_id])
                for symbol in paths
                for edge in snap._out().get(symbol, [])
                if edge.kind == EdgeKind.CALLS_API
                and edge.to_id in snap.nodes
                and snap.nodes[edge.to_id].kind == NodeKind.API_ENDPOINT
                and (snap.nodes[edge.to_id].repo_id, snap.nodes[edge.to_id].service_id)
                != (api.repo_id, api.service_id)
            ]
            for edge, target in sorted(outbound, key=lambda pair: pair[0].model_dump_json()):
                if edge.from_id in paths:
                    rows.append(_row(snap, root, api, edge, target, paths[edge.from_id], route_gap))
    return {"version": 1, "seed": seed, "services": services, "apis": apis, "rows": rows}


def apply_verdicts(report: dict[str, Any], reviewed: dict[str, Any]) -> None:
    """Apply verdicts only to identical, freshly sampled evidence rows."""
    verdicts = {row["id"]: row.get("verdict", "") for row in reviewed["rows"]}
    for verdict in verdicts.values():
        if verdict and verdict not in VERDICTS:
            raise ValueError(f"unknown review verdict: {verdict}")
    for row in report["rows"]:
        row["verdict"] = verdicts.get(row["id"], "")


def export_golden(report: dict[str, Any]) -> dict[str, Any]:
    cases = []
    for row in report["rows"]:
        if row.get("verdict") != "ok":
            continue
        cases.append(
            {
                "id": f"sample-{row['id']}",
                "api": row["api"],
                "expect": [
                    {
                        **row["target"],
                        "evidence": {
                            "file": row["call_site"]["file"],
                            "line": row["call_site"]["line"],
                        },
                    }
                ],
            }
        )
    return {"version": 1, "cases": cases}
