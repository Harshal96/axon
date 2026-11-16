"""Outbound-call resolution ladder.

Order matters: the first matching rung wins and its id is stored as the
evidence rule, so reviewers can see exactly why an edge exists.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from axon.v1.endpoint_catalog import EndpointCatalog, _literal_url, _operation_base
from axon.v1.endpoint_catalog import EndpointIndex as EndpointIndex
from axon.v1.facts import HttpFact, normalize_path
from axon.v1.graph import GraphSnapshot
from axon.v1.models import Confidence, EdgeKind, GraphEdge, GraphNode, NodeKind, endpoint_node_id
from axon.v1.servicemap import ServiceMap


def url_path(url: str) -> str:
    u = (url or "").strip()
    u = re.sub(r"^https?://[^/]+", "", u)
    u = re.sub(r"^\{[^}]*\}", "", u)  # {base_url}/v1/...
    u = re.sub(r"\{(?:env|setting|attr|var|call):[^}]*\}", "{var}", u)
    u = u.split("?")[0].split("#")[0]
    u = re.sub(r"/+", "/", u)
    return normalize_path(u) if u else ""


def operation_candidates(
    fact: HttpFact, index: EndpointCatalog, sm: ServiceMap | None = None, repo_id: str = ""
) -> list[GraphNode]:
    """Operation matching requires a distinctive name within the caller repo."""
    local = frozenset(scope for scope in index.scopes if scope[0] == repo_id)
    if sm is not None:
        for service in sm.services:
            for client in service.clients:
                identity = fact.client_id == client.id and fact.client_owner == service.id
                package = (
                    not fact.client_owner
                    and (not fact.client_id or fact.client_id == client.id)
                    and any(
                        fact.call_name == p or fact.call_name.startswith(p + ".")
                        for p in client.packages
                    )
                )
                if identity or package:
                    if not client.target_service:
                        return []
                    return [
                        ep
                        for ep in index.by_operation(
                            fact.call_name,
                            scopes=frozenset(
                                {(client.target_repo or repo_id, client.target_service)}
                            ),
                        )
                        if ep.repo_id == (client.target_repo or repo_id)
                    ]
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
    from_service: str = "",
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
                    if not client.target_service:
                        return None
                    hits = (
                        [
                            h
                            for h in index.by_method_path(
                                fact.method or "ANY",
                                url_path(fact.url),
                                scopes=frozenset(
                                    {(client.target_repo or repo_id, client.target_service)}
                                ),
                            )
                            if h.service_id == client.target_service
                        ]
                        if fact.via == "generated-source" and fact.url
                        else index.by_operation(
                            fact.call_name,
                            scopes=frozenset(
                                {(client.target_repo or repo_id, client.target_service)}
                            ),
                        )
                    )
                    hits = [h for h in hits if h.repo_id == (client.target_repo or repo_id)]
                    if len(hits) == 1:
                        return (
                            hits[0].id,
                            Confidence.INFERRED,
                            f"client:{client.id}"
                            + ("(source-path)" if fact.via == "generated-source" else ""),
                        )
                    # A declared package must never fall through to another service.
                    return None
    # Rung 1.5: explicitly declared base URL ownership.
    ref = base_reference(fact.url)
    fact.base_ref = fact.base_ref or ref
    if ref:
        services = sm.services if sm is not None else []
        # Caller-declared client bindings outrank published host ownership,
        # including when their explicit target has not been indexed yet.
        scopes = {
            (client.target_repo or repo_id, client.target_service)
            for svc in services
            for client in svc.clients
            if client.target_service
            and any(_base_matches(base, fact.url, ref) for base in client.base_urls)
        }
        if not scopes:
            scopes = set(index.literal_base_scopes(fact.url))
        # Retained literal declarations already chose the longest prefix.
        # Local service declarations provide symbolic/legacy scope only when
        # neither an explicit client nor a published literal binding exists.
        if not scopes:
            scopes = {
                (repo_id, svc.id)
                for svc in services
                if any(_base_matches(base, fact.url, ref) for base in svc.base_urls)
            }
        if scopes:
            hits = [
                ep
                for ep in index.by_method_path(
                    fact.method or "ANY", url_path(fact.url), scopes=frozenset(scopes)
                )
                if (ep.repo_id, ep.service_id) in scopes
            ]
            if len(hits) == 1:
                return (
                    hits[0].id,
                    Confidence.AMBIGUOUS if len(scopes) > 1 else Confidence.INFERRED,
                    "base-url:" + ref,
                )
            if not hits:
                literal = [
                    (ep, bound)
                    for ep, bound in index.literal_parameter(
                        fact.method, url_path(fact.url), scopes=frozenset(scopes)
                    )
                    if (ep.repo_id, ep.service_id) in scopes
                ]
                if len(literal) == 1:
                    ep, fact.bound = literal[0]
                    return ep.id, Confidence.AMBIGUOUS, "match:literal-param"
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
    snap: GraphSnapshot, edges: list[GraphEdge], sm: ServiceMap | None = None
) -> None:
    """Use distinct exact call sites as evidence, never guesses as new votes."""

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
            target = snap.nodes[edge.to_id]
            if (
                target.kind == NodeKind.API_ENDPOINT
                and not edge.evidence.rule.startswith("path-fallback:suffix")
                and not edge.evidence.rule.endswith("(many-binding)")
            ):
                groups[scope(edge)].append(edge)
    for key, links in sorted(groups.items()):
        ref = key[2]
        services = {(snap.nodes[e.to_id].repo_id, snap.nodes[e.to_id].service_id) for e in links}
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
                and (snap.nodes[edge.to_id].repo_id, snap.nodes[edge.to_id].service_id)
                == target_service
            ):
                edge.confidence = Confidence.INFERRED
            if count < 2 or edge.evidence.rule != "unresolved:path-ambiguous":
                continue
            if sm and any(
                _base_matches(base, edge.props.get("url", ""), ref)
                for svc in sm.services
                for base in [*svc.base_urls, *(b for c in svc.clients for b in c.base_urls)]
            ):
                continue
            candidate_ids = json.loads(edge.props.get("candidates", "[]"))
            candidates = [
                snap.nodes[cid]
                for cid in candidate_ids
                if cid in snap.nodes
                and (snap.nodes[cid].repo_id, snap.nodes[cid].service_id) == target_service
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
    host = re.match(r"^https?://([^/]+)", url)
    return host.group(1) if host else ""


def _base_matches(declared: str, url: str, ref: str) -> bool:
    if declared == ref:
        return True
    if declared.startswith(("env:", "setting:", "attr:")):
        return False
    target, source = _literal_url(declared), _literal_url(url)
    if target is None or source is None or target[:2] != source[:2]:
        return False
    base, path = target[2], source[2]
    return base == "/" or path == base or path.startswith(base + "/")
