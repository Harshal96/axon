"""Graph-grounded function catalog and shortest call-path evidence."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from pathlib import PurePosixPath
from typing import Any

from axon.v1.graph import GraphSnapshot
from axon.v1.models import Confidence, EdgeKind, GraphEdge, GraphNode, NodeKind, repo_node_id


def node_dict(node: GraphNode) -> dict[str, Any]:
    result = node.model_dump(mode="json")
    result["label"] = (
        f"{node.props.get('method', '')} {node.props.get('path', '')}".strip()
        if node.kind == NodeKind.API_ENDPOINT
        else node.props.get("qualified") or node.name
    )
    return result


def owner_of_node(node: GraphNode) -> dict[str, str]:
    kind = node.props.get("owner_kind") or ("service" if node.service_id else "unowned")
    root = node.props.get("owner_root") or node.file.split("/", 1)[0] or "."
    owner_id = node.service_id if kind in ("service", "client") else root
    return {
        "key": f"{node.repo_id}/{kind}/{owner_id}",
        "kind": kind,
        "id": owner_id,
        "label": f"{kind} · {owner_id}",
    }


def package_of(file: str, language: str) -> str:
    path = PurePosixPath(file)
    language = language.lower()
    if language == "python":
        parts = list(path.with_suffix("").parts)
        if parts and parts[0] == "src":
            parts.pop(0)
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)
    if language in ("java", "kotlin"):
        for prefix in ("src/main/java/", "src/main/kotlin/"):
            if prefix in file:
                return (
                    str(PurePosixPath(file.split(prefix, 1)[1]).parent).replace("/", ".").strip(".")
                )
    return str(path.parent) if str(path.parent) != "." else ""


def service_key(node: GraphNode) -> str:
    return f"{node.repo_id}/{node.service_id}"


def query_functions(
    snap: GraphSnapshot,
    *,
    q: str = "",
    repo: str = "",
    used_in: list[str] | None = None,
    defined_in: list[str] | None = None,
    kinds: list[str] | None = None,
    hide_handlers: bool = True,
    unreached: bool = False,
    sort: str = "usage",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Facets apply all filters except their own; usage is static API reach."""
    usage = snap.symbol_usage()
    endpoints = [snap.nodes[eid] for eid in usage.endpoint_ids]
    used = set(used_in or [])
    defined = set(defined_in or [])
    kind_filter = {kind.lower() for kind in kinds or []}
    outgoing = snap._out()
    symbols = snap.find_symbols(q) if len(q.strip()) >= 2 else snap.by_kind(NodeKind.SYMBOL)
    owners_by_name: dict[str, set[str]] = defaultdict(set)
    for symbol in symbols:
        if not repo or symbol.repo_id == repo:
            owners_by_name[symbol.name].add(owner_of_node(symbol)["key"])
    rows: list[dict[str, Any]] = []
    all_keys: dict[str, set[str]] = {}
    for symbol in symbols:
        if repo and symbol.repo_id != repo:
            continue
        is_handler = any(edge.kind == EdgeKind.HANDLES for edge in outgoing.get(symbol.id, []))
        if hide_handlers and is_handler:
            continue
        reached = [endpoints[i] for i in sorted(usage.eps.get(symbol.id, ()))]
        # Scope applies to both definitions and the endpoint evidence/counts.
        if repo:
            reached = [ep for ep in reached if ep.repo_id == repo]
        if unreached and reached:
            continue
        owner = owner_of_node(symbol)
        kind = symbol.props.get("kind", "function").lower()
        keys = {service_key(ep) for ep in reached}
        selected = [ep for ep in reached if not used or service_key(ep) in used]
        counts = Counter(service_key(ep) for ep in selected)
        breakdown: list[dict[str, Any]] = [
            {
                "key": key,
                "repo_id": ep.repo_id,
                "service_id": ep.service_id,
                "api_count": counts[key],
            }
            for key, ep in {service_key(ep): ep for ep in selected}.items()
        ]
        row: dict[str, Any] = {
            "symbol": node_dict(symbol),
            "owner": owner,
            "package": package_of(symbol.file, symbol.props.get("language", "")),
            "api_count": len(selected),
            "service_count": len(counts),
            "caller_count": len(usage.callers.get(symbol.id, ())),
            "used_in": sorted(breakdown, key=lambda b: b["key"]),
            "same_name_count": 1,
            "is_handler": is_handler,
            "min_depth": min(
                (
                    depth
                    for (repo_id, service_id), depth in usage.min_depth_by_service.get(
                        symbol.id, {}
                    ).items()
                    if (not repo or repo_id == repo)
                    and (not used or f"{repo_id}/{service_id}" in used)
                ),
                default=None,
            ),
            "_kind": kind,
        }
        rows.append(row)
        all_keys[symbol.id] = keys

    def matches(row: dict[str, Any], skip: str = "") -> bool:
        return (
            (skip == "used_in" or not used or bool(all_keys[row["symbol"]["id"]] & used))
            and (skip == "defined_in" or not defined or row["owner"]["key"] in defined)
            and (skip == "kind" or not kind_filter or row["_kind"] in kind_filter)
        )

    facets: dict[str, list[dict[str, Any]]] = {}
    multi_repo = not repo and len({n.repo_id for n in snap.by_kind(NodeKind.SYMBOL)}) > 1
    for facet in ("used_in", "defined_in", "kind"):
        facet_counts: Counter[str] = Counter()
        labels: dict[str, str] = {}
        for row in rows:
            if not matches(row, facet):
                continue
            symbol_data = row["symbol"]
            if facet == "used_in":
                for key in all_keys[symbol_data["id"]]:
                    facet_counts[key] += 1
                    labels[key] = key if multi_repo else key.split("/", 1)[-1]
            else:
                key = row["owner"]["key"] if facet == "defined_in" else row["_kind"]
                facet_counts[key] += 1
                labels[key] = (
                    (f"{symbol_data['repo_id']} · " if multi_repo else "") + row["owner"]["label"]
                    if facet == "defined_in"
                    else key
                )
        facets[facet] = [
            {"key": key, "label": labels[key], "count": count}
            for key, count in sorted(facet_counts.items(), key=lambda item: labels[item[0]])
        ]
    result = [row for row in rows if matches(row)]
    for row in result:
        row["same_name_count"] = len(owners_by_name[row["symbol"]["name"]])
        del row["_kind"]

    def order(row: dict[str, Any]) -> tuple[Any, ...]:
        tail = (row["symbol"]["name"].lower(), row["symbol"]["id"])
        if sort == "name":
            return tail
        if sort == "apis":
            return (-row["api_count"], -row["service_count"], -row["caller_count"], *tail)
        if sort == "callers":
            return (-row["caller_count"], -row["service_count"], -row["api_count"], *tail)
        return (-row["service_count"], -row["api_count"], -row["caller_count"], *tail)

    result.sort(key=order)
    offset = max(0, offset)
    return {
        "items": result[offset : offset + max(1, min(200, limit))],
        "total": len(result),
        "facets": facets,
    }


_STRENGTH = {
    Confidence.MANUAL: 0,
    Confidence.EXTRACTED: 0,
    Confidence.INFERRED: 1,
    Confidence.AMBIGUOUS: 2,
}


def function_detail(snap: GraphSnapshot, symbol_id: str) -> dict[str, Any]:
    """One bounded reverse BFS supplies shortest paths for all endpoint handlers."""
    symbol = snap.nodes.get(symbol_id)
    if symbol is None or symbol.kind != NodeKind.SYMBOL:
        raise KeyError(symbol_id)
    incoming = snap._in()
    parents: dict[str, GraphEdge] = {}
    distances = {symbol_id: 0}
    queue = deque([symbol_id])
    while queue:
        current = queue.popleft()
        if distances[current] >= 12:
            continue
        for edge in sorted(
            incoming.get(current, []),
            key=lambda e: (e.from_id, _STRENGTH[e.confidence], e.evidence.line),
        ):
            if edge.kind != EdgeKind.CALLS or edge.from_id in distances:
                continue
            distances[edge.from_id] = distances[current] + 1
            parents[edge.from_id] = edge
            queue.append(edge.from_id)
    apis = []
    for endpoint in snap.by_kind(NodeKind.API_ENDPOINT):
        handlers = [h for h in snap.handlers_of(endpoint.id, incoming) if h.id in distances]
        if not handlers:
            continue
        handler = min(handlers, key=lambda h: (distances[h.id], h.id))
        path = [handler]
        edges = []
        current = handler.id
        while current != symbol_id:
            edge = parents[current]
            edges.append(edge)
            current = edge.to_id
            path.append(snap.nodes[current])
        weakest = max(
            (edge.confidence for edge in edges),
            key=lambda c: _STRENGTH[c],
            default=Confidence.EXTRACTED,
        )
        apis.append(
            {
                "endpoint": node_dict(endpoint),
                "path": [node_dict(n) for n in path],
                "depth": len(edges),
                "confidence": weakest.value,
                "edges": [edge.model_dump(mode="json") for edge in edges],
            }
        )
    callers = []
    for edge in sorted(
        incoming.get(symbol_id, []), key=lambda e: (e.from_id, e.evidence.file, e.evidence.line)
    ):
        caller = snap.nodes.get(edge.from_id)
        if edge.kind == EdgeKind.CALLS and caller is not None and caller.kind == NodeKind.SYMBOL:
            callers.append(
                {
                    "symbol": node_dict(caller),
                    "owner": owner_of_node(caller),
                    "confidence": edge.confidence.value,
                    "evidence": edge.evidence.model_dump(mode="json"),
                    "rule": edge.evidence.rule,
                }
            )
    repo_node = snap.nodes.get(repo_node_id(symbol.repo_id))
    return {
        "source_path": repo_node.props.get("source_path", "") if repo_node else "",
        "symbol": node_dict(symbol),
        "owner": owner_of_node(symbol),
        "package": package_of(symbol.file, symbol.props.get("language", "")),
        "apis": apis,
        "callers": callers,
    }
