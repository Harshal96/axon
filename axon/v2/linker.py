"""Outbound-call resolution ladder.

Order matters: the first matching rung wins and its id is stored as the
evidence rule, so reviewers can see exactly why an edge exists.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from axon.v2.facts import HttpFact, normalize_path, path_signature, route_specificity
from axon.v2.graph import GraphSnapshot
from axon.v2.models import Confidence, EdgeKind, GraphEdge, GraphNode, NodeKind, endpoint_node_id
from axon.v2.servicemap import ServiceMap


def _operation_base(name: str) -> str:
    base = name.rsplit(".", 1)[-1]
    # Repeated suffixes cover WithHttpInfoAsync and snake_case siblings.
    suffixes = ("_with_streaming_response", "_with_http_info", "WithHttpInfo", "_async", "Async")
    while True:
        suffix = next((s for s in suffixes if base.lower().endswith(s.lower())), "")
        if not suffix:
            break
        base = base[: -len(suffix)]
    return base


def _variants(name: str) -> set[str]:
    base = _operation_base(name)
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower()
    camel = "".join(p.capitalize() if i else p for i, p in enumerate(base.split("_")))
    return {base.lower(), snake, camel.lower()}


def url_path(url: str) -> str:
    u = (url or "").strip()
    u = re.sub(r"^https?://[^/]+", "", u)
    u = re.sub(r"^\{[^}]*\}", "", u)  # {base_url}/v1/...
    u = re.sub(r"\{(?:env|setting|attr|var|call):[^}]*\}", "{var}", u)
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
            for v in _variants(ep.props.get("operation_id", "") or ep.name):
                if v:
                    self.by_op.setdefault(v, []).append(ep)
            literals, broad = route_specificity(ep.props.get("path", ""))
            ep.props.setdefault("literal_segments", str(literals))
            ep.props.setdefault("catch_all", str(broad).lower())
            if not self.path_target(ep):
                continue
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

    @staticmethod
    def path_target(ep: GraphNode) -> bool:
        path = ep.props.get("path", "")
        literals, broad = route_specificity(path)
        return (
            literals > 0
            and not broad
            and ep.props.get("catch_all") != "true"
            and "{unresolved}" not in path
        )

    def literal_parameter(self, method: str, path: str) -> list[tuple[GraphNode, dict[str, str]]]:
        if method.upper() not in {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "HEAD",
            "OPTIONS",
            "TRACE",
            "CONNECT",
        }:
            return []
        source = normalize_path(path).strip("/").split("/")
        hits = []
        for ep in self.endpoints:
            if not self.path_target(ep) or ep.props.get("method", "").upper() != method.upper():
                continue
            target = normalize_path(ep.props.get("path", "")).strip("/").split("/")
            if len(source) != len(target):
                continue
            bound: dict[str, str] = {}
            literals = 0
            substitutions = 0
            for left, right in zip(source, target, strict=True):
                lp, rp = re.fullmatch(r"\{[^}]+\}", left), re.fullmatch(r"\{([^}]+)\}", right)
                if lp and rp:
                    continue
                if not lp and not rp and left == right:
                    literals += 1
                elif not lp and rp:
                    substitutions += 1
                    bound[rp.group(1)] = left
                else:
                    break
            else:
                if literals >= 2 and substitutions == 1:
                    hits.append((ep, bound))
        return hits if len({(ep.repo_id, ep.service_id) for ep, _ in hits}) == 1 else []

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
        # A numeric codegen sibling is usable without source only when a spec
        # actually contains duplicate IDs. Single operations do not justify it.
        if not hits and re.search(r"_\d+$", _operation_base(call_name)):
            base = re.sub(r"_\d+$", "", _operation_base(call_name))
            deduped = [
                ep
                for ep in self.by_operation(base, service_id)
                if ep.props.get("source") in {"spec", "both"}
            ]
            counts: dict[str, int] = {}
            for ep in deduped:
                op = ep.props.get("operation_id", "")
                counts[op] = counts.get(op, 0) + 1
            hits = [ep for ep in deduped if counts.get(ep.props.get("operation_id", ""), 0) >= 2]
        # de-dupe, keep order
        seen: set[str] = set()
        out = []
        for e in hits:
            if e.id not in seen:
                seen.add(e.id)
                out.append(e)
        return out

    def by_path_suffix(self, method: str, path: str) -> list[GraphNode]:
        """Opt-in gateway matching: require a concrete verb and three segments.

        Both sides retain template markers, so a literal never matches a
        parameter. A shared suffix is evidence only, never declared ownership.
        """
        if method.upper() not in {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "HEAD",
            "OPTIONS",
            "TRACE",
            "CONNECT",
        }:
            return []
        segments = path_signature(path).strip("/").split("/")
        if len(segments) < 3:
            return []
        hits = []
        for ep in self.endpoints:
            if not self.path_target(ep) or ep.props.get("method", "").upper() != method.upper():
                continue
            target = path_signature(ep.props.get("path", "")).strip("/").split("/")
            common = 0
            for left, right in zip(reversed(segments), reversed(target), strict=False):
                if left != right:
                    break
                common += 1
            if common >= 3:
                hits.append(ep)
        return hits


def operation_candidates(
    fact: HttpFact, index: EndpointIndex, sm: ServiceMap | None = None, repo_id: str = ""
) -> list[GraphNode]:
    """Global operation matching requires a distinctive operation name."""
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
                        for ep in index.by_operation(fact.call_name, client.target_service)
                        if ep.repo_id == (client.target_repo or repo_id)
                    ]
    if fact.via not in {"generated-client", "call-through"}:
        return []
    leaf = _operation_base(fact.call_name)
    tokens = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[0-9]+", leaf)
    if len(tokens) < 2:
        return []
    return index.by_operation(fact.call_name)


def resolve_http(
    fact: HttpFact,
    index: EndpointIndex,
    sm: ServiceMap | None,
    repo_id: str,
    from_service: str = "",
    manual_sources: dict[str, str] | None = None,
) -> tuple[str, Confidence, str] | None:
    """Return (target_endpoint_id, confidence, rule) or None."""
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
                            for h in index.by_method_path(fact.method or "ANY", url_path(fact.url))
                            if h.service_id == client.target_service
                        ]
                        if fact.via == "generated-source" and fact.url
                        else index.by_operation(fact.call_name, client.target_service)
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
            if not hits:
                literal = [
                    (ep, bound)
                    for ep, bound in index.literal_parameter(fact.method, url_path(fact.url))
                    if (ep.repo_id, ep.service_id) in scopes
                ]
                if len(literal) == 1:
                    ep, fact.bound = literal[0]
                    return ep.id, Confidence.AMBIGUOUS, "match:literal-param"
            return None
    # Rung 1.6: a symbolic base without declared scope may use unique path
    # evidence. Keep it ambiguous until the caller-wide agreement pass.
    if ref.startswith(("env:", "setting:", "attr:")):
        hits = index.by_method_path(fact.method or "ANY", url_path(fact.url))
        suffix = False
        if not hits and sm is not None and sm.index.path_suffix_fallback:
            hits = index.by_path_suffix(fact.method, url_path(fact.url))
            suffix = True
        if len(hits) == 1:
            return (
                hits[0].id,
                Confidence.AMBIGUOUS,
                "path-fallback:suffix" if suffix else "path-fallback:" + ref,
            )
        if not hits:
            literal = index.literal_parameter(fact.method, url_path(fact.url))
            if len(literal) == 1:
                ep, fact.bound = literal[0]
                return ep.id, Confidence.AMBIGUOUS, "match:literal-param"
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
        if not hits:
            literal = index.literal_parameter(fact.method, path)
            if len(literal) == 1:
                ep, fact.bound = literal[0]
                return ep.id, Confidence.AMBIGUOUS, "match:literal-param"
    # Rung 3: operation-id style call name (generated clients).
    if fact.call_name:
        hits = operation_candidates(fact, index)
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
    prefix = declared.rstrip("/")
    if not prefix.startswith(("http://", "https://")):
        prefix = ("https://" if url.startswith("https://") else "http://") + prefix
    return url == prefix or url.startswith(prefix + "/")
