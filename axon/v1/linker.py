"""Outbound-call resolution ladder.

Order matters: the first matching rung wins and its id is stored as the
evidence rule, so reviewers can see exactly why an edge exists.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from axon.v1.endpoint_catalog import (
    BasePrefixes,
    EndpointCatalog,
    ServiceRef,
    _literal_url,
    _operation_base,
    matching_prefix,
)
from axon.v1.endpoint_catalog import EndpointIndex as EndpointIndex
from axon.v1.facts import HttpFact, normalize_path
from axon.v1.graph import GraphSnapshot
from axon.v1.models import Confidence, EdgeKind, GraphEdge, GraphNode, NodeKind, endpoint_node_id
from axon.v1.servicemap import ClientDecl, ServiceMap

_HTTP_AUTHORITY = re.compile(r"^(?:https?:)?//([^/?#]+)", re.IGNORECASE)


def url_path(url: str) -> str:
    u = (url or "").strip()
    has_authority = _HTTP_AUTHORITY.match(u) is not None
    u = _HTTP_AUTHORITY.sub("", u)
    u = re.sub(r"^\{[^}]*\}", "", u)  # {base_url}/v1/...
    u = re.sub(r"\{(?:env|setting|attr|var|call):[^}]*\}", "{var}", u)
    u = u.split("?")[0].split("#")[0]
    u = re.sub(r"/+", "/", u)
    return normalize_path(u) if u or has_authority else ""


def _caller_clients(
    fact: HttpFact, sm: ServiceMap | None, from_service: str | None
) -> list[ClientDecl]:
    if sm is None:
        return []
    owner = from_service
    if fact.client_id and fact.client_owner:
        # A resolved receiver can name a client declared by its source owner,
        # including a generated SDK published by another service.
        owner = fact.client_owner
    if from_service is None and not owner and len(sm.services) == 1:
        # Direct single-service resolver clients may omit the caller scope.
        owner = sm.services[0].id
    return [
        client
        for service in sm.services
        if service.id == owner and (not fact.client_owner or fact.client_owner == service.id)
        for client in service.clients
        if not fact.client_id or fact.client_id == client.id
    ]


def declared_operation_clients(
    fact: HttpFact, sm: ServiceMap | None, from_service: str | None
) -> list[ClientDecl]:
    if not fact.call_name:
        return []
    clients = _caller_clients(fact, sm, from_service)
    if fact.client_id:
        return clients
    matches = [
        (len(package), client)
        for client in clients
        for package in client.packages
        if fact.call_name == package or fact.call_name.startswith(package + ".")
    ]
    longest = max((length for length, _ in matches), default=-1)
    return list({client.id: client for length, client in matches if length == longest}.values())


def client_target_scopes(clients: list[ClientDecl], repo: str) -> frozenset[ServiceRef]:
    return frozenset(
        (client.target_repo or repo, client.target_service)
        for client in clients
        if client.target_service
    )


def declared_base_prefixes(
    fact: HttpFact,
    index: EndpointCatalog,
    sm: ServiceMap | None,
    repo: str,
    from_service: str | None,
) -> BasePrefixes:
    ref = base_reference(fact.url)
    if not ref:
        return {}
    # Caller-owned client bindings take priority even before their target is indexed.
    prefixes: BasePrefixes = defaultdict(set)
    for client in _caller_clients(fact, sm, from_service):
        if client.target_service:
            for base in client.base_urls:
                prefix = _base_prefix(base, fact.url, ref)
                if prefix is not None:
                    prefixes[(client.target_repo or repo, client.target_service)].add(prefix)
    if prefixes:
        return dict(prefixes)
    if published := index.literal_base_prefixes(fact.url):
        return published
    if sm is not None:
        for service in sm.services:
            for base in service.base_urls:
                prefix = _base_prefix(base, fact.url, ref)
                if prefix is not None:
                    prefixes[(repo, service.id)].add(prefix)
    return dict(prefixes)


def owned_path_candidates(
    fact: HttpFact, index: EndpointCatalog, prefixes: BasePrefixes
) -> list[tuple[GraphNode, dict[str, str]]]:
    """Match full and mount-relative routes within each proven ownership scope."""
    path = url_path(fact.url)
    hits: dict[str, tuple[GraphNode, dict[str, str]]] = {}
    for scope, bases in prefixes.items():
        scopes = frozenset({scope})
        relative_paths = {normalize_path(path[len(base) :]) for base in bases if base != "/"}
        paths = [path, *sorted(relative_paths - {path})]
        for candidate in sorted(paths):
            exact = index.by_method_path(fact.method or "ANY", candidate, scopes=scopes, owned=True)
            for ep, bound in [(ep, {}) for ep in exact] or index.literal_parameter(
                fact.method, candidate, scopes=scopes, owned=True
            ):
                hits.setdefault(ep.id, (ep, bound))
    return list(hits.values())


def operation_candidates(
    fact: HttpFact,
    index: EndpointCatalog,
    sm: ServiceMap | None = None,
    repo_id: str = "",
    from_service: str | None = None,
) -> list[GraphNode]:
    """Operation matching requires a distinctive name within the caller repo."""
    local = frozenset(scope for scope in index.scopes if scope[0] == repo_id)
    if clients := declared_operation_clients(fact, sm, from_service):
        return index.by_operation(fact.call_name, scopes=client_target_scopes(clients, repo_id))
    if sm is not None and fact.client_id:
        return []
    if fact.via not in {"generated-client", "call-through"}:
        return []
    leaf = _operation_base(fact.call_name)
    tokens = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[0-9]+", leaf)
    if len(tokens) < 2:
        return []
    return index.by_operation(fact.call_name, scopes=local)


def resolve_http(
    fact: HttpFact,
    index: EndpointCatalog,
    sm: ServiceMap | None,
    repo_id: str,
    from_service: str | None = None,
    manual_sources: dict[str, str] | None = None,
) -> tuple[str, Confidence, str] | None:
    """Return (target_endpoint_id, confidence, rule) or None."""
    local = frozenset(scope for scope in index.scopes if scope[0] == repo_id)
    # Nested corrections take precedence over scoped and legacy root links.
    if sm is not None:
        nested = [
            (link, svc.id) for svc in sm.services if svc.id == from_service for link in svc.links
        ]
        scoped = [(link, "") for link in sm.links if link.from_service or link.from_client]
        legacy = [(link, "") for link in sm.links if not link.from_service and not link.from_client]
        for link, scope in [*nested, *scoped, *legacy]:
            if link.from_service and link.from_service != from_service:
                continue
            if link.from_client and link.from_client != fact.client_id:
                continue
            name_hit = link.call_name and link.call_name in (
                fact.call_name,
                _leaf(fact.call_name),
                fact.call_name.rsplit(".", 1)[-1],
            )
            url_hit = link.url_contains and link.url_contains in (fact.url or "")
            if name_hit or url_hit:
                key = f"link:{scope + '/' if scope else ''}{link.id}"
                source = (manual_sources or {}).get(key, "axon.yaml")
                return (
                    endpoint_node_id(
                        link.to_repo or repo_id,
                        link.to_service,
                        link.to_method,
                        normalize_path(link.to_path),
                    ),
                    Confidence.MANUAL,
                    f"manual:{source}#{link.id}" if link.id else f"manual:{source}",
                )
    if fact.method == "UNKNOWN":
        return None
    # Rung 1: declared client target service + operation name or generated source URL.
    clients = declared_operation_clients(fact, sm, from_service)
    if clients:
        scopes = client_target_scopes(clients, repo_id)
        if len(clients) != 1 or len(scopes) != 1:
            return None
        if fact.via == "generated-source" and fact.url:
            prefixes = declared_base_prefixes(fact, index, sm, repo_id, from_service)
            scoped_hits = owned_path_candidates(
                fact, index, {scope: prefixes.get(scope, {"/"}) for scope in scopes}
            )
        else:
            scoped_hits = [(ep, {}) for ep in index.by_operation(fact.call_name, scopes=scopes)]
        if len(scoped_hits) == 1:
            ep, fact.bound = scoped_hits[0]
            return (
                ep.id,
                Confidence.INFERRED,
                f"client:{clients[0].id}"
                + ("(source-path)" if fact.via == "generated-source" else ""),
            )
        # A declared package must never fall through to another service.
        return None
    if fact.client_id and fact.call_name:
        return None
    # Rung 1.5: explicitly declared base URL ownership.
    ref = base_reference(fact.url)
    fact.base_ref = fact.base_ref or ref
    prefixes = declared_base_prefixes(fact, index, sm, repo_id, from_service)
    if prefixes:
        if len(prefixes) != 1:
            return None
        scoped_hits = owned_path_candidates(fact, index, prefixes)
        if len(scoped_hits) == 1:
            endpoint, fact.bound = scoped_hits[0]
            if fact.bound:
                return endpoint.id, Confidence.AMBIGUOUS, "match:literal-param"
            return endpoint.id, Confidence.INFERRED, "base-url:" + ref
        return None
    # An unmatched concrete authority cannot use local path or operation coincidence.
    authority = _HTTP_AUTHORITY.match(fact.url)
    if authority and "{" not in authority.group(1):
        return None
    # Rung 1.6: a symbolic base without declared scope may use unique path
    # evidence. Keep it ambiguous until the caller-wide agreement pass.
    if ref.startswith(("env:", "setting:", "attr:")):
        hits = index.by_method_path(fact.method or "ANY", url_path(fact.url), scopes=local)
        suffix = False
        if not hits and sm is not None and sm.index.path_suffix_fallback:
            hits = index.by_path_suffix(fact.method, url_path(fact.url), scopes=local)
            suffix = True
        if len(hits) == 1:
            return (
                hits[0].id,
                Confidence.AMBIGUOUS,
                "path-fallback:suffix" if suffix else "path-fallback:" + ref,
            )
        if not hits:
            literal = index.literal_parameter(fact.method, url_path(fact.url), scopes=local)
            if len(literal) == 1:
                ep, fact.bound = literal[0]
                return ep.id, Confidence.AMBIGUOUS, "match:literal-param"
        return None
    # Rung 2: method + path template.
    path = url_path(fact.url)
    if path:
        hits = index.by_method_path(fact.method or "ANY", path, scopes=local)
        if len(hits) == 1:
            return hits[0].id, Confidence.INFERRED, "match:method+path"
        if len(hits) > 1:
            if len({(h.repo_id, h.service_id) for h in hits}) > 1:
                return None
            return hits[0].id, Confidence.AMBIGUOUS, "match:method+path(many)"
        if not hits:
            literal = index.literal_parameter(fact.method, path, scopes=local)
            if len(literal) == 1:
                ep, fact.bound = literal[0]
                return ep.id, Confidence.AMBIGUOUS, "match:literal-param"
    # Rung 3: operation-id style call name (generated clients).
    if fact.call_name:
        hits = operation_candidates(fact, index, repo_id=repo_id)
        if len(hits) == 1:
            return hits[0].id, Confidence.INFERRED, "match:operationId"
        if len(hits) > 1:
            return None
    return None


def reconcile_path_fallbacks(
    snap: GraphSnapshot,
    edges: list[GraphEdge],
    *,
    catalog: EndpointCatalog | None = None,
) -> None:
    """Use distinct exact call sites as evidence, never guesses as new votes."""

    def target(node_id: str) -> GraphNode:
        node = snap.nodes.get(node_id)
        if node is None and catalog is not None:
            node = catalog.get_node(node_id)
        if node is None:
            raise ValueError(f"outbound target absent from catalog: {node_id}")
        return node

    def scope(edge: GraphEdge) -> tuple[str, str, str, str]:
        caller = snap.nodes.get(edge.from_id)
        ref = edge.props.get("base_ref", "")
        local = ""
        if ref.startswith("attr:"):
            qualified = caller.props.get("qualified", "") if caller else ""
            # self/cls fields share a class, local receivers only a function.
            owner = (
                qualified.rpartition(".")[0]
                if ref.startswith(("attr:self.", "attr:cls."))
                else qualified
            )
            local = edge.evidence.file + "::" + (owner or edge.from_id)
        return (caller.repo_id if caller else "", caller.service_id if caller else "", ref, local)

    groups: dict[tuple[str, str, str, str], list[GraphEdge]] = defaultdict(list)
    for edge in edges:
        if edge.props.get("base_ref") and (
            edge.evidence.rule.startswith("path-fallback:")
            or edge.evidence.rule.startswith("base-url:")
        ):
            endpoint = target(edge.to_id)
            if (
                endpoint.kind == NodeKind.API_ENDPOINT
                and not edge.evidence.rule.startswith("path-fallback:suffix")
                and not edge.evidence.rule.endswith("(many-binding)")
            ):
                groups[scope(edge)].append(edge)
    for key, links in sorted(groups.items()):
        ref = key[2]
        services = {(target(e.to_id).repo_id, target(e.to_id).service_id) for e in links}
        if len(services) != 1:
            names = ", ".join(f"{repo}/{service}" for repo, service in sorted(services))
            snap.diagnostics.append(f"path-fallback: conflicting targets for {ref}: {names}")
            continue
        target_service = next(iter(services))
        count = len({(e.evidence.file, e.evidence.line) for e in links})
        for edge in links:
            if edge.evidence.rule == "path-fallback:" + ref:
                edge.confidence = Confidence.INFERRED
                edge.evidence.rule += f"(consistent:{count})"
        for edge in edges:
            if scope(edge) != key or edge.evidence.rule.endswith("(many-binding)"):
                continue
            if (
                edge.evidence.rule == "match:literal-param"
                and (target(edge.to_id).repo_id, target(edge.to_id).service_id) == target_service
            ):
                edge.confidence = Confidence.INFERRED
            if count < 2 or edge.evidence.rule != "unresolved:path-ambiguous":
                continue
            if edge.props.get("target_repo") or edge.props.get("target_scope_ambiguous"):
                # An explicit client identity/package can bind ownership without
                # any base URL. Failed authoritative selection stays unresolved.
                continue
            if catalog is not None and catalog.literal_base_scopes(edge.props.get("url", "")):
                # Published ownership is a binding, including host collisions.
                # Caller-local path agreement cannot override that binding.
                continue
            candidate_ids = json.loads(edge.props.get("candidates", "[]"))
            candidates = [
                target(cid)
                for cid in candidate_ids
                if (
                    cid in snap.nodes or (catalog is not None and catalog.get_node(cid) is not None)
                )
                and (target(cid).repo_id, target(cid).service_id) == target_service
            ]
            if (
                len(candidates) != 1
                or candidates[0].props.get("method", "").upper()
                != edge.props.get("method", "").upper()
            ):
                continue
            edge.to_id = candidates[0].id
            edge.evidence.rule = f"path-fallback:{ref}(consensus:{count})"
            edge.confidence = Confidence.AMBIGUOUS
            snap.diagnostics.append(
                f"base consensus: {ref} -> {target_service[1]} from {count} call sites"
            )
            marker = (
                f"unresolved outbound call {edge.evidence.file}:{edge.evidence.line} "
                + edge.props.get("call_name", "")
                + " "
                + edge.props.get("url", "")
            )
            if marker.strip() in snap.unresolved:
                snap.unresolved.remove(marker.strip())
    # Remove orphaned placeholders after a successful consensus, preserving shared ones.
    snap.prune_orphaned_externals()


def base_url_suggestions(snap: GraphSnapshot) -> list[str]:
    """Read-only suggestions, with counts and sample source locations."""
    groups: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in snap.edges:
        if edge.kind == EdgeKind.CALLS_API and edge.evidence.rule.startswith("path-fallback:"):
            ref = edge.props.get("base_ref", "")
            if ref:
                groups[ref].append(edge)
    suggestions = []
    for ref, links in sorted(groups.items()):
        targets = {(snap.nodes[e.to_id].repo_id, snap.nodes[e.to_id].service_id) for e in links}
        if len(targets) != 1 or any(
            e.evidence.rule.startswith("path-fallback:suffix") for e in links
        ):
            continue
        _, service = next(iter(targets))
        sites = sorted({f"{e.evidence.file}:{e.evidence.line}" for e in links})
        suggestions.append(
            f"suggested base_urls: [{ref}] on service {service}: {len(sites)} call site(s); "
            + ", ".join(sites[:3])
            + "; review before declaring"
        )
    return suggestions


def _leaf(dotted: str) -> str:
    parts = dotted.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else dotted


def base_reference(url: str) -> str:
    match = re.match(r"^\{((?:env|setting|attr):[^}]+)\}", url)
    if match:
        return match.group(1)
    host = _HTTP_AUTHORITY.match(url)
    return host.group(1).lower() if host else ""


def _base_prefix(declared: str, url: str, ref: str) -> str | None:
    if declared.startswith(("env:", "setting:", "attr:")):
        return "/" if declared == ref else None
    base = _literal_url(declared)
    call = _literal_url(url, allow_template_path=True)
    if base is None or call is None:
        return None
    return matching_prefix(base, call)
