"""In-memory graph snapshot + API-first traversals.

The same query functions serve the memory store, snapshot files loaded by
the CLI/Action, and snapshots hydrated from Neo4j.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from axon.v2.models import EdgeKind, GraphEdge, GraphNode, NodeKind


class GraphSnapshot(BaseModel):
    nodes: dict[str, GraphNode] = Field(default_factory=dict)
    edges: list[GraphEdge] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    # Parse diagnostics from target code; not persisted to Neo4j.
    diagnostics: list[str] = Field(default_factory=list)
    commit: str = ""

    _kind_cache: dict[NodeKind, list[GraphNode]] = PrivateAttr(default_factory=dict)
    # Lazily built edge indexes; None means dirty. Cleared by add_edge,
    # dedupe_edges, and any direct edges-list mutation (store.save).
    _out_index: dict[str, list[GraphEdge]] | None = PrivateAttr(default=None)
    _in_index: dict[str, list[GraphEdge]] | None = PrivateAttr(default=None)
    _calls_adj: dict[str, list[str]] | None = PrivateAttr(default=None)
    _calls_rev: dict[str, list[str]] | None = PrivateAttr(default=None)
    _api_adj: dict[str, list[tuple[GraphEdge, GraphNode]]] | None = PrivateAttr(default=None)
    _closure_cache: dict[tuple[str, int], dict[str, int]] = PrivateAttr(default_factory=dict)
    _targets_memo: dict[tuple[str, int], frozenset[tuple[str, str]]] = PrivateAttr(
        default_factory=dict
    )

    def _invalidate_edge_indexes(self) -> None:
        self._out_index = None
        self._in_index = None
        self._calls_adj = None
        self._calls_rev = None
        self._api_adj = None
        self._closure_cache.clear()
        self._targets_memo.clear()

    # -- construction ------------------------------------------------------
    def add_node(self, node: GraphNode) -> GraphNode:
        self._kind_cache.clear()
        existing = self.nodes.get(node.id)
        if existing is not None:
            # Merge props; first non-empty scalar wins.
            for k, v in node.props.items():
                existing.props.setdefault(k, v)
            if not existing.name:
                existing.name = node.name
            return existing
        self.nodes[node.id] = node
        return node

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges.append(edge)
        if self._out_index is not None or self._in_index is not None:
            self._invalidate_edge_indexes()

    def dedupe_edges(self) -> None:
        seen: set[tuple[str, str, str, int]] = set()
        out: list[GraphEdge] = []
        for e in self.edges:
            key = (e.from_id, e.to_id, e.kind.value, e.evidence.line)
            if key not in seen:
                seen.add(key)
                out.append(e)
        self.edges = out
        self._invalidate_edge_indexes()

    # -- indexes ------------------------------------------------------------
    # Built once per snapshot state. Previously _out()/_in() rebuilt the
    # full edge index on every call, which made per-endpoint traversals
    # O(endpoints x edges) and wedged the API server on large graphs.
    def _out(self) -> dict[str, list[GraphEdge]]:
        if self._out_index is None:
            idx: dict[str, list[GraphEdge]] = defaultdict(list)
            for e in self.edges:
                idx[e.from_id].append(e)
            self._out_index = idx
        return self._out_index

    def _in(self) -> dict[str, list[GraphEdge]]:
        if self._in_index is None:
            idx: dict[str, list[GraphEdge]] = defaultdict(list)
            for e in self.edges:
                idx[e.to_id].append(e)
            self._in_index = idx
        return self._in_index

    def _build_call_adjacency(self) -> None:
        """CALLS-only forward/reverse adjacency plus per-symbol API targets."""
        calls_adj: dict[str, list[str]] = defaultdict(list)
        calls_rev: dict[str, list[str]] = defaultdict(list)
        api_adj: dict[str, list[tuple[GraphEdge, GraphNode]]] = defaultdict(list)
        for e in self.edges:
            if e.kind == EdgeKind.CALLS:
                calls_adj[e.from_id].append(e.to_id)
                calls_rev[e.to_id].append(e.from_id)
            elif e.kind == EdgeKind.CALLS_API:
                target = self.nodes.get(e.to_id)
                if target is not None:
                    api_adj[e.from_id].append((e, target))
        self._calls_adj = calls_adj
        self._calls_rev = calls_rev
        self._api_adj = api_adj

    def _targets_within(self, sym: str, k: int) -> frozenset[tuple[str, str]]:
        """(caller_sym, target_id) CALLS_API pairs reachable within k CALLS hops.

        Memoized on (sym, k): shared library subtrees are computed once
        per remaining depth instead of once per endpoint handler.
        Recursion depth is bounded by k (<= 12 by callers).
        """
        if self._api_adj is None:
            self._build_call_adjacency()
        assert self._calls_adj is not None and self._api_adj is not None
        key = (sym, k)
        if key in self._targets_memo:
            return self._targets_memo[key]
        result: set[tuple[str, str]] = {
            (sym, target.id) for _e, target in self._api_adj.get(sym, [])
        }
        if k > 0:
            for callee in self._calls_adj.get(sym, []):
                result |= self._targets_within(callee, k - 1)
        frozen = frozenset(result)
        self._targets_memo[key] = frozen
        return frozen

    def by_kind(self, kind: NodeKind) -> list[GraphNode]:
        if kind not in self._kind_cache:
            self._kind_cache[kind] = sorted(
                (n for n in self.nodes.values() if n.kind == kind),
                key=lambda n: n.id,
            )
        return self._kind_cache[kind]

    # -- lookups --------------------------------------------------------------
    def find_endpoints(self, query: str) -> list[GraphNode]:
        q = query.strip().lower()
        scored: list[tuple[int, GraphNode]] = []
        for n in self.by_kind(NodeKind.API_ENDPOINT):
            hay = " ".join(
                [
                    n.id,
                    n.name,
                    n.props.get("path", ""),
                    n.props.get("operation_id", ""),
                    n.props.get("method", ""),
                    n.service_id,
                ]
            ).lower()
            if q and q in hay:
                score = 3 if n.props.get("path", "").lower() == q else 2
                if n.props.get("operation_id", "").lower() == q:
                    score = 3
                scored.append((score, n))
            elif q and all(part in hay for part in q.split() if len(part) > 1):
                scored.append((1, n))
        scored.sort(key=lambda t: (-t[0], t[1].id))
        return [n for _, n in scored]

    def find_symbols(self, query: str) -> list[GraphNode]:
        q = query.strip().lower()
        out = []
        for n in self.by_kind(NodeKind.SYMBOL):
            if q in n.name.lower() or q in n.props.get("qualified", "").lower():
                out.append(n)
        out.sort(key=lambda n: (n.name.lower() != q, n.id))
        return out

    # -- traversal helpers ------------------------------------------------------
    def handlers_of(
        self, endpoint_id: str, _in: dict[str, list[GraphEdge]] | None = None
    ) -> list[GraphNode]:
        inn = self._in() if _in is None else _in
        ids = dict.fromkeys(
            e.from_id for e in inn.get(endpoint_id, []) if e.kind == EdgeKind.HANDLES
        )
        return [self.nodes[sid] for sid in ids if sid in self.nodes]

    def handler_of(
        self, endpoint_id: str, _in: dict[str, list[GraphEdge]] | None = None
    ) -> GraphNode | None:
        handlers = self.handlers_of(endpoint_id, _in)
        return handlers[0] if handlers else None

    def coverage_by_service(self, repo_id: str = "") -> dict[str, dict[str, int]]:
        """Per-service link coverage: endpoints with a handler, and
        outbound CALLS_API edges split resolved vs ExternalApi.

        Cheap edge counts (no closure walks), so this is safe to run on
        every index result even for very large graphs.
        """
        handled = {e.to_id for e in self.edges if e.kind == EdgeKind.HANDLES}
        out: dict[str, dict[str, int]] = {}
        for ep in self.by_kind(NodeKind.API_ENDPOINT):
            if repo_id and ep.repo_id != repo_id:
                continue
            cov = out.setdefault(
                ep.service_id,
                {
                    "endpoints": 0,
                    "with_handler": 0,
                    "outbound_resolved": 0,
                    "outbound_external": 0,
                },
            )
            cov["endpoints"] += 1
            if ep.id in handled:
                cov["with_handler"] += 1
        for e in self.edges:
            if e.kind != EdgeKind.CALLS_API:
                continue
            caller = self.nodes.get(e.from_id)
            target = self.nodes.get(e.to_id)
            if caller is None or target is None:
                continue
            if caller.props.get("owner_kind") == "client":
                continue
            if repo_id and caller.repo_id != repo_id:
                continue
            cov = out.setdefault(
                caller.service_id,
                {
                    "endpoints": 0,
                    "with_handler": 0,
                    "outbound_resolved": 0,
                    "outbound_external": 0,
                },
            )
            if target.kind == NodeKind.API_ENDPOINT:
                cov["outbound_resolved"] += 1
            else:
                cov["outbound_external"] += 1
        return out

    def call_closure(self, start_symbol_id: str, max_depth: int = 12) -> dict[str, int]:
        """BFS over Symbol -CALLS-> Symbol. Returns {symbol_id: depth}.

        Memoized per (symbol, max_depth); callers get a copy.
        """
        key = (start_symbol_id, max_depth)
        if key in self._closure_cache:
            return dict(self._closure_cache[key])
        out = self._out()
        depths: dict[str, int] = {start_symbol_id: 0}
        queue: deque[tuple[str, int]] = deque([(start_symbol_id, 0)])
        while queue:
            cur, d = queue.popleft()
            if d >= max_depth:
                continue
            for e in out.get(cur, []):
                if e.kind == EdgeKind.CALLS and e.to_id not in depths:
                    depths[e.to_id] = d + 1
                    queue.append((e.to_id, d + 1))
        self._closure_cache[key] = depths
        return dict(depths)

    def outbound_calls(self, endpoint_id: str) -> list[tuple[GraphEdge, GraphNode]]:
        """(edge, target endpoint/external) reachable from the handler closure."""
        handlers = self.handlers_of(endpoint_id)
        if not handlers:
            return []
        if self._api_adj is None:
            self._build_call_adjacency()
        assert self._api_adj is not None
        edge_by_pair: dict[tuple[str, str], GraphEdge] = {}
        found: list[tuple[GraphEdge, GraphNode]] = []
        pairs = set().union(*(self._targets_within(h.id, 12) for h in handlers))
        for sym_id, target_id in pairs:
            if target_id == endpoint_id:
                continue
            if (sym_id, target_id) not in edge_by_pair:
                for e, _t in self._api_adj.get(sym_id, []):
                    edge_by_pair.setdefault((sym_id, e.to_id), e)
            edge = edge_by_pair.get((sym_id, target_id))
            target = self.nodes.get(target_id)
            if edge is not None and target is not None:
                found.append((edge, target))
        found.sort(key=lambda t: (t[1].id, t[0].from_id))
        return found

    def outbound_endpoints(self, endpoint_id: str, transitive: bool = False) -> list[GraphNode]:
        direct = list({t.id: t for _, t in self.outbound_calls(endpoint_id)}.values())
        if not transitive:
            return direct
        seen = {endpoint_id}
        result: list[GraphNode] = []
        queue: deque[str] = deque(n.id for n in direct)
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            node = self.nodes.get(cur)
            if node is not None:
                result.append(node)
                if node.kind == NodeKind.API_ENDPOINT:
                    queue.extend(t.id for _, t in self.outbound_calls(cur))
        return result

    def inbound_callers(self, endpoint_id: str) -> list[tuple[GraphEdge, GraphNode]]:
        """Symbols that call this endpoint, with their owning endpoint if any."""
        inn = self._in()
        found = []
        for e in inn.get(endpoint_id, []):
            if e.kind == EdgeKind.CALLS_API:
                caller = self.nodes.get(e.from_id)
                if caller is not None:
                    found.append((e, caller))
        found.sort(key=lambda t: t[1].id)
        return found

    def endpoint_of_symbol(self, symbol_id: str) -> list[GraphNode]:
        """Endpoints whose handler closure contains this symbol.

        One reverse BFS from the symbol (callers within depth 12)
        instead of a forward closure per endpoint: symbol is in
        closure(handler) iff handler reaches it, i.e. handler is in
        the symbol's reverse closure at the same distance bound.
        """
        if self._calls_rev is None:
            self._build_call_adjacency()
        assert self._calls_rev is not None
        ancestors: dict[str, int] = {symbol_id: 0}
        queue: deque[tuple[str, int]] = deque([(symbol_id, 0)])
        while queue:
            cur, d = queue.popleft()
            if d >= 12:
                continue
            for caller in self._calls_rev.get(cur, []):
                if caller not in ancestors:
                    ancestors[caller] = d + 1
                    queue.append((caller, d + 1))
        inn = self._in()
        result = []
        for ep in self.by_kind(NodeKind.API_ENDPOINT):
            if any(h.id in ancestors for h in self.handlers_of(ep.id, _in=inn)):
                result.append(ep)
        return result

    def api_graph(
        self,
        endpoint_id: str,
        depth: int = 2,
        max_symbols: int = 80,
        max_callers: int = 50,
        max_owners_per_caller: int = 4,
    ) -> dict[str, Any]:
        """Bounded ego-graph; outbound paths are admitted whole or omitted.

        Traversal metadata counts reachable symbols/callers before truncation.
        The caller band has an independent hard budget of 80 nodes, and the
        outbound target band is bounded by the symbol budget as well.
        """
        ep = self.nodes.get(endpoint_id)
        if ep is None:
            return {
                "endpoint": None,
                "nodes": [],
                "edges": [],
                "truncated": False,
                "total_symbols": 0,
                "total_callers": 0,
            }
        max_symbols = max(1, min(max_symbols, 500))
        max_callers = max(0, min(max_callers, 80))
        max_owners_per_caller = max(0, min(max_owners_per_caller, 10))
        nodes: dict[str, GraphNode] = {ep.id: ep}
        edges: list[dict[str, Any]] = []
        framework_evidence = {
            (e.from_id, e.to_id, e.kind): e.evidence.model_dump()
            for e in self.edges
            if e.evidence.rule.startswith("framework:")
        }
        seen_edges: set[tuple[str, str, str]] = set()
        truncated = False

        def link(a: str, b: str, kind: str) -> None:
            key = (a, b, kind)
            if key not in seen_edges and a in nodes and b in nodes:
                seen_edges.add(key)
                item: dict[str, Any] = {"from_id": a, "to_id": b, "kind": kind}
                if key in framework_evidence:
                    item["evidence"] = framework_evidence[key]
                edges.append(item)

        handlers = self.handlers_of(endpoint_id)
        out = self._out()
        # Multi-source BFS provides deterministic shortest path predecessors.
        parents: dict[str, str | None] = {h.id: None for h in handlers}
        distances = {h.id: 0 for h in handlers}
        queue = deque(parents)
        while queue:
            cur = queue.popleft()
            if distances[cur] >= 12:
                continue
            for edge in sorted(out.get(cur, []), key=lambda e: (e.to_id, e.kind)):
                if edge.kind != EdgeKind.CALLS or edge.to_id in parents:
                    continue
                if edge.to_id not in self.nodes:
                    continue
                parents[edge.to_id] = cur
                distances[edge.to_id] = distances[cur] + 1
                queue.append(edge.to_id)
        selected = {h.id for h in handlers[:max_symbols]}
        outbound = self.outbound_calls(endpoint_id)
        accepted_outbound: list[tuple[GraphEdge, GraphNode]] = []
        target_ids: set[str] = set()
        for edge, target in sorted(
            outbound,
            key=lambda pair: (distances.get(pair[0].from_id, 13), pair[1].id, pair[0].from_id),
        ):
            spine: set[str] = set()
            ancestor: str | None = edge.from_id
            while ancestor is not None and ancestor in parents:
                spine.add(ancestor)
                ancestor = parents[ancestor]
            if (
                not spine
                or len(selected | spine) > max_symbols
                or (target.id not in target_ids and len(target_ids) >= max_symbols)
            ):
                truncated = True
                continue
            selected |= spine
            target_ids.add(target.id)
            accepted_outbound.append((edge, target))
        # Fill with shallow helpers only after preserving outbound spines.
        display_depth = min(12, max(1, depth * 3))
        for sid in parents:
            if len(selected) >= max_symbols:
                break
            parent = parents[sid]
            if distances[sid] <= display_depth and (parent is None or parent in selected):
                selected.add(sid)
        truncated |= len(selected) < len(parents)
        for sid in parents:
            if sid in selected:
                nodes[sid] = self.nodes[sid]
        for handler in handlers:
            link(handler.id, ep.id, "HANDLES")
        for sid in parents:
            if sid not in selected:
                continue
            for edge in out.get(sid, []):
                if edge.kind == EdgeKind.CALLS and edge.to_id in selected:
                    link(sid, edge.to_id, "CALLS")
        for edge, target in accepted_outbound:
            nodes[target.id] = target
            link(edge.from_id, target.id, "CALLS_API")

        callers = self.inbound_callers(endpoint_id)
        caller_ids = list(dict.fromkeys(c.id for _, c in callers))
        accepted_callers = caller_ids[:max_callers]
        band: set[str] = set(accepted_callers)
        for sid in accepted_callers:
            nodes[sid] = self.nodes[sid]
            link(sid, ep.id, "CALLS_API")
            owners = self.endpoint_of_symbol(sid)
            if len(owners) > max_owners_per_caller:
                truncated = True
            for owner in owners[:max_owners_per_caller]:
                if owner.id not in band and len(band) >= 80:
                    truncated = True
                    continue
                band.add(owner.id)
                nodes[owner.id] = owner
                link(owner.id, sid, "HANDLES_CHAIN")
        truncated |= len(accepted_callers) < len(caller_ids)
        svc = self.nodes.get(f"service::{ep.repo_id}::{ep.service_id}")
        if svc is not None:
            nodes[svc.id] = svc
            link(svc.id, ep.id, "EXPOSES")
        return {
            "endpoint": ep.model_dump(mode="json"),
            "nodes": [n.model_dump(mode="json") for n in nodes.values()],
            "edges": edges,
            "truncated": truncated,
            "total_symbols": len(parents),
            "total_callers": len(caller_ids),
        }
