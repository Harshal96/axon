"""Read-only maintenance views grounded in snapshot rows."""

from __future__ import annotations

import json
from typing import Any

from axon.v1.graph import GraphSnapshot
from axon.v1.models import EdgeKind, NodeKind


def _fix(rule: str) -> str:
    if "client" in rule or "factory" in rule:
        return "declare-client"
    if "url" in rule or "host" in rule:
        return "add-base-url"
    if "ambiguous" in rule:
        return "scoped-link"
    return "parser-gap"


def api_explain(snap: GraphSnapshot, repo: str, api: str, service: str = "") -> dict[str, Any]:
    matches = [
        n
        for n in snap.by_kind(NodeKind.API_ENDPOINT)
        if n.repo_id == repo
        and (not service or n.service_id == service)
        and (n.id == api or f"{n.props.get('method', '')} {n.props.get('path', '')}" == api)
    ]
    if not matches:
        raise KeyError(f"unknown API: {api}")
    if len(matches) != 1:
        return {"status": "ambiguous", "candidates": [n.model_dump(mode="json") for n in matches]}
    ep = matches[0]
    handlers = snap.handlers_of(ep.id)
    closure = set().union(*(set(snap.call_closure(h.id)) for h in handlers))
    outbound, unresolved = [], []
    for edge in snap.edges:
        if edge.kind != EdgeKind.CALLS_API or edge.from_id not in closure:
            continue
        node = snap.nodes.get(edge.to_id)
        row = edge.model_dump(mode="json")
        row["target"] = node.model_dump(mode="json") if node else None
        if node is None or node.kind == NodeKind.EXTERNAL_API:
            row["rule_bucket"] = edge.evidence.rule or "unknown"
            row["suggested_fix"] = _fix(edge.evidence.rule)
            try:
                candidates = json.loads(edge.props.get("candidates", "[]"))
            except (ValueError, TypeError):
                candidates = []
            row["candidates"] = candidates if isinstance(candidates, list) else []
            unresolved.append(row)
        else:
            outbound.append(row)
    return {
        "status": "ok",
        "endpoint": ep.model_dump(mode="json"),
        "handler": handlers[0].model_dump(mode="json") if len(handlers) == 1 else None,
        "handlers": [h.model_dump(mode="json") for h in handlers],
        "handler_edges": [
            e.model_dump(mode="json")
            for e in snap.edges
            if e.kind == EdgeKind.HANDLES and e.to_id == ep.id
        ],
        "outbound": outbound,
        "unresolved": unresolved,
        "unknowns": [
            "Dynamic calls and parser omissions without graph rows are unknown; "
            "closure depth is limited to 12."
        ],
    }


def client_list(snap: GraphSnapshot, repo: str) -> list[dict[str, Any]]:
    return [
        client_show(snap, repo, n.name)
        for n in snap.by_kind(NodeKind.API_CLIENT)
        if n.repo_id == repo
    ]


def client_show(snap: GraphSnapshot, repo: str, identity: str) -> dict[str, Any]:
    node = next(
        (
            n
            for n in snap.by_kind(NodeKind.API_CLIENT)
            if n.repo_id == repo and (n.name == identity or n.id == identity)
        ),
        None,
    )
    if node is None:
        raise KeyError(f"unknown client: {identity}")
    rules = (f"client:{node.name}", f"client:{node.name}:")
    edges = [
        e
        for e in snap.edges
        if e.kind == EdgeKind.CALLS_API
        and (
            e.evidence.rule == rules[0]
            or e.evidence.rule.startswith(rules[1])
            or e.evidence.rule.startswith(rules[0] + "(")
        )
        and snap.nodes.get(e.from_id)
        and snap.nodes[e.from_id].repo_id == repo
    ]
    return {
        "client": node.model_dump(mode="json"),
        "consumers": sorted({snap.nodes[e.from_id].service_id for e in edges}),
        "linked": sum(
            snap.nodes.get(e.to_id) is not None
            and snap.nodes[e.to_id].kind == NodeKind.API_ENDPOINT
            for e in edges
        ),
        "unresolved": sum(
            snap.nodes.get(e.to_id) is None or snap.nodes[e.to_id].kind == NodeKind.EXTERNAL_API
            for e in edges
        ),
        "edges": [e.model_dump(mode="json") for e in edges],
        "unknowns": ["Consumers and counts include only graph edges carrying this client's rule."],
    }


def service_show(snap: GraphSnapshot, repo: str, identity: str) -> dict[str, Any]:
    node = next(
        (
            n
            for n in snap.by_kind(NodeKind.SERVICE)
            if n.repo_id == repo and n.service_id == identity
        ),
        None,
    )
    if node is None:
        raise KeyError(f"unknown service: {identity}")
    return {
        "service": node.model_dump(mode="json"),
        "clients": [
            n.model_dump(mode="json")
            for n in snap.by_kind(NodeKind.API_CLIENT)
            if n.repo_id == repo and n.service_id == identity
        ],
        "coverage": snap.coverage_by_service(repo).get(identity, {}),
        "resolution": snap.resolution_report(repo).get(identity, {}),
    }


def graph_refs(snap: GraphSnapshot, repo: str, kind: str, identity: str) -> dict[str, Any]:
    kinds = {
        "service": NodeKind.SERVICE,
        "client": NodeKind.API_CLIENT,
        "endpoint": NodeKind.API_ENDPOINT,
    }
    if kind not in kinds:
        raise ValueError("kind must be service, client, or endpoint")
    ids = {
        n.id
        for n in snap.by_kind(kinds[kind])
        if n.repo_id == repo
        and (
            n.id == identity
            or n.name == identity
            or (kind == "service" and n.service_id == identity)
        )
    }
    return {
        "entity": f"{kind}:{identity}",
        "nodes": sorted(ids),
        "graph_references": [
            e.model_dump(mode="json") for e in snap.edges if e.from_id in ids or e.to_id in ids
        ],
        "unknowns": [
            "Config references require a local checkout; graph references are snapshot rows only."
        ],
    }
