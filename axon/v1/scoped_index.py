"""Fresh-parse selection and focused reporting; publication remains repository-wide."""

from pathlib import Path
from typing import Any

from axon.v1.graph import GraphSnapshot
from axon.v1.graph_delta import GraphDelta, edge_key
from axon.v1.graphql_inputs import graphql_dependency_paths
from axon.v1.models import EdgeKind, GraphNode, NodeKind, endpoint_label
from axon.v1.servicemap import ServiceMap, owner_of


def scoped_paths(
    root: Path, sm: ServiceMap, services: list[str], snapshot: GraphSnapshot | None = None
) -> set[str]:
    from axon.v1.indexer import collect_files

    unknown = set(services) - {service.id for service in sm.services}
    if unknown:
        raise ValueError("unknown services: " + ", ".join(sorted(unknown)))
    selected = set(services)
    if not selected:
        return set()
    files = set(collect_files(root, sm))
    wanted = {path for path in files if owner_of(sm, path)[1] in selected}
    if snapshot is not None:
        for edge in snapshot.edges:
            if "graphql_dependencies" in edge.props and any(
                (node := snapshot.nodes.get(node_id)) is not None
                and node.repo_id == sm.repo
                and node.service_id in selected
                for node_id in (edge.from_id, edge.to_id)
            ):
                wanted.update(graphql_dependency_paths(edge))
                wanted.add(edge.evidence.file)
    return wanted & files


def focused_result(
    before: GraphSnapshot, after: GraphSnapshot, repo: str, services: list[str]
) -> dict[str, Any]:
    selected = set(services)

    def outbound(snapshot: GraphSnapshot) -> GraphSnapshot:
        return GraphSnapshot(
            edges=[
                edge
                for edge in snapshot.edges
                if edge.kind == EdgeKind.CALLS_API
                and (node := snapshot.nodes.get(edge.from_id)) is not None
                and node.repo_id == repo
                and node.service_id in selected
            ]
        )

    old = outbound(before)
    delta = GraphDelta.between(old, outbound(after))
    return {
        "services": list(dict.fromkeys(services)),
        "coverage": {
            key: value for key, value in after.coverage_by_service(repo).items() if key in selected
        },
        "resolution": {
            key: value for key, value in after.resolution_report(repo).items() if key in selected
        },
        "graph_delta": {
            "added": [edge.model_dump(mode="json") for edge in delta.add_edges],
            "removed": [
                edge.model_dump(mode="json")
                for edge in old.edges
                if edge_key(edge) in delta.remove_edge_keys
            ],
        },
    }


def resolve_endpoint(
    snap: GraphSnapshot, api_ref: str, service: str = "", repo: str = ""
) -> GraphNode:
    """Resolve an endpoint ID, HTTP method/path, or case-sensitive GraphQL coordinate."""
    ref = api_ref.strip()
    node = snap.nodes.get(ref)
    if node is not None and node.kind == NodeKind.API_ENDPOINT:
        if (service and node.service_id != service) or (repo and node.repo_id != repo):
            raise ValueError(f"API outside requested scope: {api_ref}")
        return node
    matches = [
        n
        for n in snap.by_kind(NodeKind.API_ENDPOINT)
        if (not service or n.service_id == service)
        and (not repo or n.repo_id == repo)
        and ref == endpoint_label(n)
    ]
    if not matches:
        raise ValueError(f"unknown API: {api_ref}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous API: {api_ref}; use the full endpoint id")
    return matches[0]


def endpoint_files(snap: GraphSnapshot, endpoint_id: str) -> set[str]:
    """Files whose fresh parse can change this API's handler/outbound edges."""
    files: set[str] = set()
    ep = snap.nodes.get(endpoint_id)
    if ep is None:
        return files
    if ep.file:
        files.add(ep.file)
    handlers = snap.handlers_of(endpoint_id)
    closure: set[str] = set()
    for handler in handlers:
        closure.update(snap.call_closure(handler.id))
        if handler.file:
            files.add(handler.file)
    for symbol_id in closure:
        node = snap.nodes.get(symbol_id)
        if node is not None and node.file:
            files.add(node.file)
    for edge in snap.edges:
        relevant = (
            (edge.kind in (EdgeKind.HANDLES, EdgeKind.DEFINES) and edge.to_id == endpoint_id)
            or (
                edge.kind == EdgeKind.CALLS_API
                and (edge.to_id == endpoint_id or edge.from_id in closure)
            )
            or (edge.kind == EdgeKind.CALLS and edge.from_id in closure)
        )
        if relevant:
            files.update(graphql_dependency_paths(edge))
            if edge.evidence.file:
                files.add(edge.evidence.file)
    return files


def endpoint_scoped_paths(
    root: Path, sm: ServiceMap, snap: GraphSnapshot, endpoint_id: str
) -> set[str]:
    from axon.v1.indexer import collect_files

    wanted = endpoint_files(snap, endpoint_id)
    if not wanted:
        return set()
    return wanted & set(collect_files(root, sm))


def _relevant_api_edges(snap: GraphSnapshot, endpoint_id: str) -> list[Any]:
    handlers = snap.handlers_of(endpoint_id)
    closure: set[str] = set()
    for handler in handlers:
        closure.update(snap.call_closure(handler.id))
    return [
        edge
        for edge in snap.edges
        if (edge.kind in (EdgeKind.HANDLES, EdgeKind.DEFINES) and edge.to_id == endpoint_id)
        or (edge.kind == EdgeKind.CALLS_API and edge.to_id == endpoint_id)
        or (edge.kind == EdgeKind.CALLS_API and edge.from_id in closure)
    ]


def focused_api_result(
    before: GraphSnapshot, after: GraphSnapshot, endpoint_id: str
) -> dict[str, Any]:
    old = GraphSnapshot(edges=_relevant_api_edges(before, endpoint_id))
    new = GraphSnapshot(edges=_relevant_api_edges(after, endpoint_id))
    delta = GraphDelta.between(old, new)
    ep = after.nodes.get(endpoint_id)
    return {
        "api": endpoint_id,
        "service": ep.service_id if ep else "",
        "repo": ep.repo_id if ep else "",
        "changed": bool(
            delta.add_edges or delta.remove_edge_keys or before.nodes.get(endpoint_id) != ep
        ),
        "handlers": [h.model_dump(mode="json") for h in after.handlers_of(endpoint_id)],
        "outbound": [n.model_dump(mode="json") for n in after.outbound_endpoints(endpoint_id)],
        "callers": [n.model_dump(mode="json") for _, n in after.inbound_callers(endpoint_id)],
        "graph_delta": {
            "added": [edge.model_dump(mode="json") for edge in delta.add_edges],
            "removed": [
                edge.model_dump(mode="json")
                for edge in old.edges
                if edge_key(edge) in delta.remove_edge_keys
            ],
        },
    }


def evict_scoped_facts(root: Path, paths: set[str]) -> None:
    """Remove content-addressed entries before a guaranteed fresh parse."""
    from axon.v1.factscache import _entry_path, cache_dir, cache_key
    from axon.v1.indexer import PARSER_VERSIONS

    for path in paths:
        suffix = Path(path).suffix
        if suffix not in PARSER_VERSIONS:
            continue
        try:
            key = cache_key(suffix, PARSER_VERSIONS[suffix], (root / path).read_bytes())
            _entry_path(key, cache_dir()).unlink(missing_ok=True)
        except OSError:
            # Fresh tasks bypass the cache even when removing an entry fails.
            continue
