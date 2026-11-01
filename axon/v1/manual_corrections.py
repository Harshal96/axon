"""Apply evidenced, service-scoped corrections after native extraction."""

from collections.abc import Callable
from pathlib import Path

from axon.v1.facts import normalize_path
from axon.v1.graph import GraphSnapshot
from axon.v1.models import (
    Confidence,
    EdgeKind,
    Evidence,
    GraphEdge,
    GraphNode,
    NodeKind,
    endpoint_node_id,
    external_node_id,
    service_node_id,
)
from axon.v1.servicemap import ServiceMap, load_source_map


def manual_sources(root: Path) -> dict[str, str]:
    return {
        key: Path(locations[-1].file).resolve().relative_to(root.resolve()).as_posix()
        for key, locations in load_source_map(root).entities.items()
        if locations
    }


def provenance(sources: dict[str, str], key: str, entry_id: str) -> tuple[str, dict[str, str]]:
    source = sources.get(key, "axon.yaml")
    return f"manual:{source}#{entry_id}", {"source_file": source, "entry_id": entry_id}


def api_id(repo: str, service: str, api: str) -> str:
    method, _, path = api.strip().partition(" ")
    return endpoint_node_id(repo, service, method, normalize_path(path.strip()))


def add_manual_routes(
    snap: GraphSnapshot, repo: str, sm: ServiceMap, sources: dict[str, str]
) -> None:
    from axon.v1.indexer import _find_symbol, _symbol_index

    index = _symbol_index(snap, repo)
    # Include file-qualified Python symbols without guessing across services.
    for node in snap.by_kind(NodeKind.SYMBOL):
        if node.repo_id == repo:
            module = node.file.removesuffix(".py").replace("/", ".")
            index.setdefault(module + "." + node.props.get("qualified", node.name), []).append(node)
    for svc in sm.services:
        for entry in svc.endpoints:
            eid = endpoint_node_id(repo, svc.id, entry.method, normalize_path(entry.path))
            rule, props = provenance(sources, f"endpoint:{svc.id}/{entry.id}", entry.id)
            if eid in snap.nodes:
                snap.diagnostics.append(f"redundant-endpoint: {svc.id}/{entry.id} found natively")
                continue
            snap.add_node(
                GraphNode(
                    id=eid,
                    kind=NodeKind.API_ENDPOINT,
                    name=f"{entry.method.upper()} {normalize_path(entry.path)}",
                    repo_id=repo,
                    service_id=svc.id,
                    props={
                        "method": entry.method.upper(),
                        "path": normalize_path(entry.path),
                        "source": "manual",
                        "rule": "manual:endpoint",
                        **props,
                    },
                )
            )
            snap.add_edge(
                GraphEdge(
                    from_id=service_node_id(repo, svc.id),
                    to_id=eid,
                    kind=EdgeKind.EXPOSES,
                    confidence=Confidence.MANUAL,
                    evidence=Evidence(rule=rule),
                    props=props,
                )
            )
            _set_handler(snap, index, svc.id, eid, entry.handler, rule, props, _find_symbol)
        for override in svc.handlers:
            eid = api_id(repo, svc.id, override.api)
            rule, props = provenance(sources, f"handler:{svc.id}/{override.id}", override.id)
            if eid not in snap.nodes:
                snap.diagnostics.append(
                    f"unmatched-handler: {svc.id}/{override.id}: {override.api}"
                )
                continue
            _set_handler(snap, index, svc.id, eid, override.handler, rule, props, _find_symbol)


def _set_handler(
    snap: GraphSnapshot,
    index: dict[str, list[GraphNode]],
    service: str,
    eid: str,
    handler: str,
    rule: str,
    props: dict[str, str],
    find_symbol: Callable[[dict[str, list[GraphNode]], str, str], str | None],
) -> None:
    scoped = {key: [n for n in nodes if n.service_id == service] for key, nodes in index.items()}
    candidates = {node.id for node in scoped.get(handler, [])}
    hid = find_symbol(scoped, handler, service) if len(candidates) == 1 else None
    if hid is None:
        snap.diagnostics.append(f"unresolved-handler: {rule}: {handler}")
        return
    snap.edges = [e for e in snap.edges if not (e.kind == EdgeKind.HANDLES and e.to_id == eid)]
    snap._invalidate_edge_indexes()
    snap.add_edge(
        GraphEdge(
            from_id=hid,
            to_id=eid,
            kind=EdgeKind.HANDLES,
            confidence=Confidence.MANUAL,
            evidence=Evidence(rule=rule),
            props=props,
        )
    )


def add_manual_calls(
    snap: GraphSnapshot, repo: str, sm: ServiceMap, sources: dict[str, str]
) -> None:
    for svc in sm.services:
        for entry in svc.calls:
            eid = api_id(repo, svc.id, entry.from_api)
            handlers = [
                e.from_id for e in snap.edges if e.kind == EdgeKind.HANDLES and e.to_id == eid
            ]
            if not handlers:
                snap.diagnostics.append(f"unmatched-call: {svc.id}/{entry.id}: {entry.from_api}")
                continue
            target = entry.to
            tid = endpoint_node_id(
                target.repo or repo, target.service, target.method, normalize_path(target.path)
            )
            if tid not in snap.nodes:
                template = f"{target.repo or repo}/{target.service}:{normalize_path(target.path)}"
                tid = external_node_id(target.method, template)
                snap.add_node(
                    GraphNode(
                        id=tid,
                        kind=NodeKind.EXTERNAL_API,
                        name=f"{target.method} {template}",
                        repo_id=target.repo or repo,
                        service_id=target.service,
                        props={
                            "method": target.method.upper(),
                            "path": normalize_path(target.path),
                            "target_service": target.service,
                        },
                    )
                )
                snap.unresolved.append(
                    f"declared call target not indexed: {svc.id}/{entry.id}: {template}"
                )
            rule, props = provenance(sources, f"call:{svc.id}/{entry.id}", entry.id)
            for hid in handlers:
                snap.add_edge(
                    GraphEdge(
                        from_id=hid,
                        to_id=tid,
                        kind=EdgeKind.CALLS_API,
                        confidence=Confidence.MANUAL,
                        evidence=Evidence(
                            file=entry.evidence.file,
                            line=entry.evidence.line,
                            detail=entry.evidence.text,
                            rule=rule,
                        ),
                        props=props,
                    )
                )


def suppress_edges(snap: GraphSnapshot, repo: str, sm: ServiceMap) -> None:
    for svc in sm.services:
        for entry in svc.suppress:
            roots = (
                [
                    e.from_id
                    for e in snap.edges
                    if e.kind == EdgeKind.HANDLES
                    and e.to_id == api_id(repo, svc.id, entry.from_api)
                ]
                if entry.from_api
                else []
            )
            closure = {sid for root in roots for sid in snap.call_closure(root)} | set(roots)
            removed = []
            for edge in snap.edges:
                caller, target = snap.nodes.get(edge.from_id), snap.nodes.get(edge.to_id)
                if edge.kind != EdgeKind.CALLS_API or caller is None or target is None:
                    continue
                if caller.repo_id != repo:
                    continue
                if not entry.from_api and caller.service_id != svc.id:
                    continue
                if entry.from_api and edge.from_id not in closure:
                    continue
                if entry.to_service and target.service_id != entry.to_service:
                    continue
                if entry.from_client and not (
                    edge.props.get("client_id") == entry.from_client
                    or edge.evidence.rule.startswith(f"client:{entry.from_client}")
                ):
                    continue
                removed.append(edge)
            snap.edges = [e for e in snap.edges if e not in removed]
            snap._invalidate_edge_indexes()
            code = "suppressed:manual" if removed else "unmatched-suppress"
            snap.diagnostics.append(f"{code}#{entry.id}: {len(removed)} edge(s): {entry.reason}")
    snap.prune_orphaned_externals()
