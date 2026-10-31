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
    u = u.split("?")[0].split("#")[0]
    u = re.sub(r"/+", "/", u)
    return normalize_path(u) if u else ""


class EndpointIndex:
    def __init__(self, snap: GraphSnapshot) -> None:
        self.endpoints = snap.by_kind(NodeKind.API_ENDPOINT)
        self.by_sig: dict[tuple[str, str], list[GraphNode]] = {}
        self.by_op: dict[str, list[GraphNode]] = {}
        self.by_suffix: dict[tuple[str, str], list[GraphNode]] = {}
        for ep in self.endpoints:
            method = ep.props.get("method", "")
            sig = path_signature(ep.props.get("path", ""))
            self.by_sig.setdefault((method, sig), []).append(ep)
            self.by_sig.setdefault(("ANY", sig), []).append(ep)
            self.by_sig.setdefault(("", sig), []).append(ep)
            base_path = normalize_path(ep.props.get("base_path", ""))
            if base_path != "/" and sig.startswith(base_path.rstrip("/") + "/"):
                alias = sig[len(base_path.rstrip("/")) :]
                for key in {(method, alias), ("ANY", alias), ("", alias)}:
                    self.by_suffix.setdefault(key, []).append(ep)
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
        # Preserve spec base paths while supporting call paths with or without
        # gateway prefixes. Concrete methods never broaden to other verbs.
        suffix_hits = list(self.by_suffix.get((method.upper(), sig), []))
        if sig.startswith("/api/"):
            suffix_hits.extend(self.by_sig.get((method.upper(), sig[4:]), []))
        return list({ep.id: ep for ep in suffix_hits}.values())

    def by_operation(self, call_name: str, service_id: str = "") -> list[GraphNode]:
        hits: list[GraphNode] = []
        for v in _variants(call_name):
            hits.extend(self.by_op.get(v, []))
        if service_id:
            scoped = [e for e in hits if e.service_id == service_id]
            hits = scoped
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
                package_match = any(
                    fact.call_name == p or fact.call_name.startswith(p + ".")
                    for p in client.packages
                )
                identity_match = fact.client_id == client.id and fact.client_owner == svc.id
                if identity_match or (
                    not fact.client_owner
                    and package_match
                    and (not fact.client_id or fact.client_id == client.id)
                ):
                    hits = index.by_operation(fact.call_name, client.target_service)
                    hits = [h for h in hits if h.repo_id == (client.target_repo or repo_id)]
                    if len(hits) == 1:
                        return hits[0].id, Confidence.INFERRED, f"client:{client.id}"
                    # A declared package must never fall through to another service.
                    return None
    # Rung 1.5: explicitly declared base URL ownership.
    ref = base_reference(fact.url)
    fact.base_ref = fact.base_ref or ref
    if sm is not None and ref:
        scopes: set[tuple[str, str]] = set()
        for svc in sm.services:
            if any(_base_matches(base, fact.url, ref) for base in svc.base_urls):
                scopes.add((repo_id, svc.id))
            for client in svc.clients:
                if (
                    any(_base_matches(base, fact.url, ref) for base in client.base_urls)
                    and client.target_service
                ):
                    scopes.add((client.target_repo or repo_id, client.target_service))
        if scopes:
            hits = [
                ep
                for ep in index.by_method_path(fact.method or "ANY", url_path(fact.url))
                if (ep.repo_id, ep.service_id) in scopes
            ]
            if len(hits) == 1:
                return (
                    hits[0].id,
                    Confidence.AMBIGUOUS if len(scopes) > 1 else Confidence.INFERRED,
                    "base-url:" + ref,
                )
            return None
        if ref.startswith(("env:", "setting:", "attr:")):
            return None
    # Rung 2: method + path template.
    path = url_path(fact.url)
    if path:
        hits = index.by_method_path(fact.method or "ANY", path)
        if len(hits) == 1:
            return hits[0].id, Confidence.INFERRED, "match:method+path"
        if len(hits) > 1:
            if len({(h.repo_id, h.service_id) for h in hits}) > 1:
                return None
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


def base_reference(url: str) -> str:
    match = re.match(r"^\{((?:env|setting|attr):[^}]+)\}", url)
    if match:
        return match.group(1)
    host = re.match(r"^https?://([^/]+)", url)
    return host.group(1) if host else ""


def _base_matches(declared: str, url: str, ref: str) -> bool:
    if declared == ref:
        return True
    if declared.startswith(("env:", "setting:", "attr:")):
        return False
    prefix = declared.rstrip("/")
    if not prefix.startswith(("http://", "https://")):
        prefix = ("https://" if url.startswith("https://") else "http://") + prefix
    return url == prefix or url.startswith(prefix + "/")
