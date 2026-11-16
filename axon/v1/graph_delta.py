"""Copy-on-write graph changes; logical node identifiers remain unchanged.

Published snapshots are read-only by convention. Unchanged model objects are
shared, while containers and traversal caches always belong to one publication.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from axon.v1.graph import GraphSnapshot
from axon.v1.link_inputs import RepoLinkInputs, RepoLinkReport
from axon.v1.models import GraphEdge, GraphNode, NodeKind


def edge_key(edge: GraphEdge) -> str:
    """Complete edge identity, including distinct evidence and properties."""
    import json

    return json.dumps(edge.model_dump(mode="json"), sort_keys=True)


@dataclass
class GraphDelta:
    upsert_nodes: dict[str, GraphNode] = field(default_factory=dict)
    remove_node_ids: set[str] = field(default_factory=set)
    add_edges: list[GraphEdge] = field(default_factory=list)
    remove_edge_keys: set[str] = field(default_factory=set)
    unresolved: list[str] | None = None
    diagnostics: list[str] | None = None
    commit: str | None = None
    link_inputs: dict[str, RepoLinkInputs] | None = None
    link_reports: dict[str, RepoLinkReport] | None = None
    publication_id: str | None = None

    def apply(self, base: GraphSnapshot) -> GraphSnapshot:
        nodes = {k: v for k, v in base.nodes.items() if k not in self.remove_node_ids}
        nodes.update(self.upsert_nodes)
        edges = [
            e
            for e in base.edges
            if e.from_id not in self.remove_node_ids
            and e.to_id not in self.remove_node_ids
            and (not self.remove_edge_keys or edge_key(e) not in self.remove_edge_keys)
        ]
        edges.extend(self.add_edges)
        return GraphSnapshot(
            schema_version=base.schema_version,
            nodes=nodes,
            edges=edges,
            unresolved=list(base.unresolved if self.unresolved is None else self.unresolved),
            diagnostics=list(base.diagnostics if self.diagnostics is None else self.diagnostics),
            commit=base.commit if self.commit is None else self.commit,
            link_inputs=deepcopy(
                base.link_inputs if self.link_inputs is None else self.link_inputs
            ),
            link_reports=deepcopy(
                base.link_reports if self.link_reports is None else self.link_reports
            ),
            publication_id=base.publication_id
            if self.publication_id is None
            else self.publication_id,
        )

    @classmethod
    def between(cls, base: GraphSnapshot, target: GraphSnapshot) -> GraphDelta:
        """Diff complete snapshots; changed edges retain all evidence fields."""
        from hashlib import sha256

        # Hold fixed-size fingerprints instead of two serialized copies of all
        # evidence/property payloads. Exact keys are needed only for removals.
        before = {sha256(edge_key(e).encode()).digest(): e for e in base.edges}
        after = {sha256(edge_key(e).encode()).digest(): e for e in target.edges}
        return cls(
            upsert_nodes={k: v for k, v in target.nodes.items() if base.nodes.get(k) != v},
            remove_node_ids=base.nodes.keys() - target.nodes.keys(),
            add_edges=[e for k, e in after.items() if k not in before],
            remove_edge_keys={edge_key(before[key]) for key in before.keys() - after.keys()},
            unresolved=target.unresolved,
            diagnostics=target.diagnostics,
            commit=target.commit,
            link_inputs=target.link_inputs,
            link_reports=target.link_reports,
            publication_id=target.publication_id,
        )


def replace_repositories(
    base: GraphSnapshot, incoming: GraphSnapshot, *, repo_ids: set[str] | None = None
) -> GraphSnapshot:
    """Prepare a replacement without touching containers or models in base."""
    repos = (
        repo_ids
        if repo_ids is not None
        else {n.repo_id for n in incoming.nodes.values() if n.kind == NodeKind.REPO and n.repo_id}
    )
    drop = {k for k, n in base.nodes.items() if n.repo_id in repos}
    nodes = {k: n for k, n in base.nodes.items() if k not in drop}
    nodes.update(incoming.nodes)
    edges = [
        e for e in base.edges if e.from_id not in drop and e.from_id in nodes and e.to_id in nodes
    ]
    edges.extend(e for e in incoming.edges if e.from_id in nodes and e.to_id in nodes)
    result = GraphDelta(
        upsert_nodes=incoming.nodes,
        remove_node_ids=drop,
        add_edges=edges,
        remove_edge_keys={edge_key(e) for e in base.edges},
        unresolved=incoming.unresolved,
        diagnostics=incoming.diagnostics,
        commit=incoming.commit,
        link_inputs={
            **{k: v for k, v in base.link_inputs.items() if k not in repos},
            **incoming.link_inputs,
        },
        link_reports={
            **{k: v for k, v in base.link_reports.items() if k not in repos},
            **incoming.link_reports,
        },
        publication_id=incoming.publication_id or base.publication_id,
    ).apply(base)

    referenced = {endpoint for e in result.edges for endpoint in (e.from_id, e.to_id)}
    result.nodes = {
        key: node
        for key, node in result.nodes.items()
        if node.kind != NodeKind.EXTERNAL_API or node.repo_id or key in referenced
    }
    return result
