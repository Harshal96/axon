"""Outbound-call resolution ladder.

Order matters: the first matching rung wins and its id is stored as the
evidence rule, so reviewers can see exactly why an edge exists.
"""

from __future__ import annotations

import re

from axon.v2.facts import HttpFact, normalize_path, path_signature
from axon.v2.graph import GraphSnapshot
from axon.v2.models import Confidence, GraphNode, NodeKind, endpoint_node_id
from axon.v2.servicemap import ServiceMap


def _variants(name: str) -> set[str]:
    base = name.rsplit(".", 1)[-1]
    # Strip swagger-codegen sibling-method suffixes so
    # v1_x_with_http_info / v1_x_async resolve to operation v1_x.
    for suffix in ("_with_http_info", "_async", "_with_streaming_response"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower()
    camel = "".join(p.capitalize() if i else p for i, p in enumerate(base.split("_")))
    return {base.lower(), snake, camel.lower()}


def url_path(url: str) -> str:
    u = (url or "").strip()
    u = re.sub(r"^https?://[^/]+", "", u)
    u = re.sub(r"^\{[^}]*\}", "", u)  # {base_url}/v1/...
    u = u.split("?")[0]
    return normalize_path(u) if u else ""


class EndpointIndex:
    def __init__(self, snap: GraphSnapshot) -> None:
        self.endpoints = snap.by_kind(NodeKind.API_ENDPOINT)
        self.by_sig: dict[tuple[str, str], list[GraphNode]] = {}
        self.by_op: dict[str, list[GraphNode]] = {}
        for ep in self.endpoints:
            method = ep.props.get("method", "")
            sig = path_signature(ep.props.get("path", ""))
            self.by_sig.setdefault((method, sig), []).append(ep)
            self.by_sig.setdefault(("ANY", sig), []).append(ep)
            self.by_sig.setdefault(("", sig), []).append(ep)
            for v in _variants(ep.props.get("operation_id", "") or ep.name):
                if v:
                    self.by_op.setdefault(v, []).append(ep)

    def by_method_path(self, method: str, path: str) -> list[GraphNode]:
        if not path:
            return []
        sig = path_signature(path)
        out = self.by_sig.get((method.upper(), sig), [])
        if out:
            return out
        # Suffix match: call url may carry a gateway prefix the spec lacks.
        # Try every "/" boundary suffix of the call signature against the
        # index instead of scanning every endpoint signature per call.
        seen: set[str] = set()
        suffix_hits: list[GraphNode] = []
        for i in range(1, len(sig)):
            if sig[i] != "/":
                continue
            suffix = sig[i:]
            if suffix == "/":
                continue
            for m in (method.upper(), "ANY", ""):
                for ep in self.by_sig.get((m, suffix), []):
                    if ep.id not in seen:
                        seen.add(ep.id)
                        suffix_hits.append(ep)
        return suffix_hits

    def by_operation(self, call_name: str, service_id: str = "") -> list[GraphNode]:
        hits: list[GraphNode] = []
        for v in _variants(call_name):
            hits.extend(self.by_op.get(v, []))
        if service_id:
            scoped = [e for e in hits if e.service_id == service_id]
            return scoped or hits
        # de-dupe, keep order
        seen: set[str] = set()
        out = []
        for e in hits:
            if e.id not in seen:
                seen.add(e.id)
                out.append(e)
        return out


def resolve_http(
    fact: HttpFact,
    index: EndpointIndex,
    sm: ServiceMap | None,
    repo_id: str,
) -> tuple[str, Confidence, str] | None:
    """Return (target_endpoint_id, confidence, rule) or None."""
    # Rung 0: manual override in axon.yaml.
    if sm is not None:
        for link in sm.links:
            name_hit = link.call_name and link.call_name in (
                fact.call_name,
                _leaf(fact.call_name),
                fact.call_name.rsplit(".", 1)[-1],
            )
            url_hit = link.url_contains and link.url_contains in (fact.url or "")
            if name_hit or url_hit:
                target_repo = link.to_repo or repo_id
                return (
                    endpoint_node_id(
                        target_repo, link.to_service, link.to_method, normalize_path(link.to_path)
                    ),
                    Confidence.MANUAL,
                    "manual:axon.yaml",
                )
    # Rung 1: declared client target service + operation name.
    if sm is not None and fact.call_name:
        for svc in sm.services:
            for client in svc.clients:
                if client.packages and any(fact.call_name.startswith(p) for p in client.packages):
                    hits = index.by_operation(fact.call_name, client.target_service)
                    hits = [
                        h for h in hits if not client.target_repo or h.repo_id == client.target_repo
                    ]
                    if len(hits) == 1:
                        return hits[0].id, Confidence.INFERRED, f"client:{client.id}"
    # Rung 2: method + path template.
    path = url_path(fact.url)
    if path:
        hits = index.by_method_path(fact.method, path)
        if len(hits) == 1:
            return hits[0].id, Confidence.INFERRED, "match:method+path"
        if len(hits) > 1:
            return hits[0].id, Confidence.AMBIGUOUS, "match:method+path(many)"
    # Rung 3: operation-id style call name (generated clients).
    if fact.call_name:
        hits = index.by_operation(fact.call_name)
        if len(hits) == 1:
            return hits[0].id, Confidence.INFERRED, "match:operationId"
        if len(hits) > 1:
            return hits[0].id, Confidence.AMBIGUOUS, "match:operationId(many)"
    return None


def _leaf(dotted: str) -> str:
    parts = dotted.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else dotted
