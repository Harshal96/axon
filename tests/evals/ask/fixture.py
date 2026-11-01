"""Hand-authored graph with deliberate English-word symbol collisions."""

from axon.v2.graph import GraphSnapshot
from axon.v2.models import EdgeKind, Evidence, GraphEdge, GraphNode, NodeKind


def snapshot() -> GraphSnapshot:
    snap = GraphSnapshot()
    for sid, name in [
        ("scheduler", "Scheduler"),
        ("directory", "Directory"),
        ("ledger", "Ledger"),
        ("search-east", "Search"),
        ("search-west", "Search"),
        ("ux", "UX"),
    ]:
        snap.add_node(
            GraphNode(
                id=f"service::sample::{sid}",
                kind=NodeKind.SERVICE,
                repo_id="sample",
                service_id=sid,
                name=name,
            )
        )
    for sid, method, path, handler in [
        ("scheduler", "GET", "/runs/{id}", "run"),
        ("scheduler", "POST", "/flags", "flag"),
        ("scheduler", "GET", "/idle", "idle"),
        ("scheduler", "GET", "/orphan", ""),
        ("directory", "GET", "/entries/{id}", "entry"),
        ("directory", "POST", "/evaluate", "evaluate"),
        ("ledger", "GET", "/invoices", "invoice"),
    ]:
        eid = f"endpoint::sample::{sid}::{method}:{path}"
        snap.add_node(
            GraphNode(
                id=eid,
                kind=NodeKind.API_ENDPOINT,
                name=f"{method} {path}",
                repo_id="sample",
                service_id=sid,
                file=f"{sid}/views.py",
                line_start=10,
                props={"method": method, "path": path},
            )
        )
        if handler:
            snap.add_node(
                GraphNode(
                    id=handler,
                    kind=NodeKind.SYMBOL,
                    name=handler,
                    repo_id="sample",
                    service_id=sid,
                    file=f"{sid}/views.py",
                    line_start=12,
                )
            )
            snap.add_edge(GraphEdge(from_id=handler, to_id=eid, kind=EdgeKind.HANDLES))
    for name in ["Directory", "Service", "price_cart", "EntryEdges.from_entry_id"]:
        snap.add_node(
            GraphNode(
                id=name,
                kind=NodeKind.SYMBOL,
                name=name.split(".")[-1],
                repo_id="sample",
                props={"qualified": name},
            )
        )
    for caller, target in [
        ("run", "endpoint::sample::directory::GET:/entries/{id}"),
        ("flag", "endpoint::sample::directory::POST:/evaluate"),
    ]:
        snap.add_edge(
            GraphEdge(
                from_id=caller,
                to_id=target,
                kind=EdgeKind.CALLS_API,
                evidence=Evidence(file="scheduler/views.py", line=22, rule="call-through"),
            )
        )
    snap.add_edge(GraphEdge(from_id="run", to_id="price_cart", kind=EdgeKind.CALLS))
    snap.add_edge(GraphEdge(from_id="flag", to_id="EntryEdges.from_entry_id", kind=EdgeKind.CALLS))
    snap.add_node(
        GraphNode(
            id="external",
            kind=NodeKind.EXTERNAL_API,
            name="GET directory/search",
            props={"url": "https://directory.invalid/search"},
        )
    )
    snap.add_edge(
        GraphEdge(
            from_id="run",
            to_id="external",
            kind=EdgeKind.CALLS_API,
            evidence=Evidence(rule="unresolved"),
        )
    )
    return snap
