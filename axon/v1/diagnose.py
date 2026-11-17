"""Read-only maintenance views grounded in snapshot rows."""

from __future__ import annotations

import json
from typing import Any

from axon.v1.graph import GraphSnapshot
from axon.v1.models import EdgeKind, GraphEdge, NodeKind, repo_node_id


def _fix(rule: str) -> str:
    if "client" in rule or "factory" in rule:
        return "declare-client"
    if "url" in rule or "host" in rule:
        return "add-base-url"
    if "ambiguous" in rule:
        return "scoped-link"
    return "parser-gap"


def matching_status(snap: GraphSnapshot, repo: str, service: str = "") -> dict[str, Any]:
    inputs_missing = repo not in snap.link_inputs
    pinned = sum(
        edge.kind == EdgeKind.CALLS_API
        and edge.from_id in snap.nodes
        and snap.nodes[edge.from_id].repo_id == repo
        and (not service or snap.nodes[edge.from_id].service_id == service)
        and (inputs_missing or edge.props.get("relink_status") == "pinned-legacy")
        for edge in snap.edges
    )
    return {"pinned_legacy": pinned, "fresh_matching_unknown": inputs_missing or bool(pinned)}


def link_diagnosis(snap: GraphSnapshot, edge: GraphEdge) -> dict[str, Any]:
    """Classify this call site using published scope, never shared placeholder scope."""
    node = snap.nodes.get(edge.to_id)
    repo, service = edge.props.get("target_repo", ""), edge.props.get("target_service", "")
    try:
        candidates = json.loads(edge.props.get("candidates", "[]"))
    except (ValueError, TypeError):
        candidates = []
    candidates = (
        [v for v in candidates if isinstance(v, str)] if isinstance(candidates, list) else []
    )
    if node and node.props.get("kind") == "sdk":
        reason = "external-dependency"
    elif edge.props.get("relink_status") == "pinned-legacy":
        reason = "pinned-legacy"
    elif node and node.kind == NodeKind.API_ENDPOINT:
        reason = "resolved"
    elif repo and repo_node_id(repo) not in snap.nodes:
        reason = "target-repo-missing"
    elif (
        repo
        and service
        and not any(
            n.repo_id == repo and n.service_id == service for n in snap.by_kind(NodeKind.SERVICE)
        )
    ):
        reason = "target-service-missing"
    elif len(candidates) > 1:
        reason = "target-ambiguous"
    elif repo and service:
        reason = "target-endpoint-missing"
    else:
        reason = "unbound-target"
    return {
        "reason": reason,
        "target_repo": repo,
        "target_service": service,
        "candidates": candidates,
        "candidate_nodes": [
            snap.nodes[c].model_dump(mode="json") for c in candidates if c in snap.nodes
        ],
    }


def base_url_suggestions(
    snap: GraphSnapshot, caller_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[GraphEdge]] = {}
    for edge in snap.edges:
        caller = snap.nodes.get(edge.from_id)
        if (
            edge.kind != EdgeKind.CALLS_API
            or caller is None
            or (caller_ids is not None and edge.from_id not in caller_ids)
            or not edge.evidence.rule.startswith("path-fallback:")
            or edge.evidence.rule.startswith("path-fallback:suffix")
            or edge.props.get("relink_status") == "pinned-legacy"
        ):
            continue
        ref = edge.props.get("base_ref", "")
        if ref:
            groups.setdefault((caller.repo_id, caller.service_id, ref), []).append(edge)
    suggestions = []
    for (repo, service, ref), edges in sorted(groups.items()):
        targets = {
            (snap.nodes[e.to_id].repo_id, snap.nodes[e.to_id].service_id)
            for e in edges
            if e.to_id in snap.nodes and snap.nodes[e.to_id].kind == NodeKind.API_ENDPOINT
        }
        if len(targets) != 1 or len(edges) != sum(
            e.to_id in snap.nodes and snap.nodes[e.to_id].kind == NodeKind.API_ENDPOINT
            for e in edges
        ):
            continue
        target_repo, target_service = next(iter(targets))
        suggestions.append(
            {
                "repo_id": repo,
                "service_id": service,
                "base_ref": ref,
                "target_repo": target_repo,
                "target_service": target_service,
                "call_sites": sorted({f"{e.evidence.file}:{e.evidence.line}" for e in edges}),
            }
        )
    return suggestions


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
        return {
            "status": "ambiguous",
            "publication_id": snap.publication_id,
            "candidates": [n.model_dump(mode="json") for n in matches],
        }
    ep = matches[0]
    handlers = snap.handlers_of(ep.id)
    closure = set().union(*(set(snap.call_closure(h.id)) for h in handlers))
    outbound, unresolved, dependencies = [], [], []
    unknowns = [
        "Dynamic calls and parser omissions without graph rows are unknown; "
        "closure depth is limited to 12."
    ]
    state = matching_status(snap, repo, ep.service_id)
    if state["fresh_matching_unknown"]:
        unknowns.append(
            "link-inputs-missing: pinned-legacy rows and coverage cannot prove fresh matching; "
            "re-index the caller repository."
        )
    for edge in snap.edges:
        if edge.kind != EdgeKind.CALLS_API or edge.from_id not in closure:
            continue
        node = snap.nodes.get(edge.to_id)
        row = edge.model_dump(mode="json")
        row["target"] = node.model_dump(mode="json") if node else None
        row.update(link_diagnosis(snap, edge))
        if row["reason"] == "pinned-legacy":
            unknowns.append(
                "link-inputs-missing: pinned-legacy call evidence cannot prove fresh "
                "matching or coverage."
            )
        if row["reason"] == "external-dependency":
            dependencies.append(row)
            continue
        if node is None or node.kind == NodeKind.EXTERNAL_API:
            row["rule_bucket"] = edge.evidence.rule or "unknown"
            row["suggested_fix"] = _fix(edge.evidence.rule)
            unresolved.append(row)
        else:
            outbound.append(row)
    return {
        "status": "ok",
        **state,
        "publication_id": snap.publication_id,
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
        "external_dependencies": dependencies,
        "base_url_suggestions": base_url_suggestions(snap, closure),
        "unknowns": list(dict.fromkeys(unknowns)),
    }


def client_list(snap: GraphSnapshot, repo: str) -> list[dict[str, Any]]:
    return [
        client_show(snap, repo, n.id)
        for n in snap.by_kind(NodeKind.API_CLIENT)
        if n.repo_id == repo
    ]


def client_show(snap: GraphSnapshot, repo: str, identity: str) -> dict[str, Any]:
    matches = [
        n
        for n in snap.by_kind(NodeKind.API_CLIENT)
        if n.repo_id == repo and (n.name == identity or n.id == identity)
    ]
    if not matches:
        raise KeyError(f"unknown client: {identity}")
    if len(matches) != 1:
        return {
            "status": "ambiguous",
            "publication_id": snap.publication_id,
            "candidates": [n.model_dump(mode="json") for n in matches],
        }
    node = matches[0]
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
        and (snap.nodes[e.from_id].repo_id, snap.nodes[e.from_id].service_id)
        == (repo, node.service_id)
    ]
    return {
        "publication_id": snap.publication_id,
        **matching_status(snap, repo, node.service_id),
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
        "publication_id": snap.publication_id,
        **matching_status(snap, repo, node.service_id),
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
        "publication_id": snap.publication_id,
        "entity": f"{kind}:{identity}",
        "nodes": sorted(ids),
        "graph_references": [
            e.model_dump(mode="json") for e in snap.edges if e.from_id in ids or e.to_id in ids
        ],
        "unknowns": [
            "Config references require a local checkout; graph references are snapshot rows only."
        ],
    }
