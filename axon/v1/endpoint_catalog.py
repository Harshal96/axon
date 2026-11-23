"""Immutable endpoint catalog and scoped matching."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit

from axon.v1.facts import normalize_path, path_signature, route_specificity
from axon.v1.graph import GraphSnapshot
from axon.v1.models import GraphNode, NodeKind


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


class _EndpointMatcher:
    def __init__(self, snap: GraphSnapshot) -> None:
        self.endpoints = snap.by_kind(NodeKind.API_ENDPOINT)
        self.by_sig: dict[tuple[str, str], list[GraphNode]] = {}
        self.by_op: dict[str, list[GraphNode]] = {}
        self.by_suffix: dict[tuple[str, str], list[GraphNode]] = {}
        self.roots: list[GraphNode] = []
        for ep in self.endpoints:
            for v in _variants(ep.props.get("operation_id", "") or ep.name):
                if v:
                    self.by_op.setdefault(v, []).append(ep)
            if ep.props.get("path") == "/" and ep.props.get("catch_all") != "true":
                self.roots.append(ep)
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

    def literal_parameter(
        self, method: str, path: str, *, owned: bool = False
    ) -> list[tuple[GraphNode, dict[str, str]]]:
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
                if literals >= (1 if owned else 2) and substitutions == 1:
                    hits.append((ep, bound))
        return hits if len({(ep.repo_id, ep.service_id) for ep, _ in hits}) == 1 else []

    def by_method_path(self, method: str, path: str, *, owned: bool = False) -> list[GraphNode]:
        if not path:
            return []
        sig = path_signature(path)
        if owned and sig == "/":
            return [ep for ep in self.roots if ep.props.get("method", "") == method.upper()]
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
            counts: dict[tuple[str, str, str], int] = {}
            for ep in deduped:
                op = (ep.repo_id, ep.service_id, ep.props.get("operation_id", ""))
                counts[op] = counts.get(op, 0) + 1
            hits = [
                ep
                for ep in deduped
                if counts.get((ep.repo_id, ep.service_id, ep.props.get("operation_id", "")), 0) >= 2
            ]
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


ServiceRef = tuple[str, str]
BasePrefixes = dict[ServiceRef, set[str]]


@dataclass(frozen=True)
class LiteralHttpUrl:
    scheme: str
    host: str
    port: int | None
    path: str


def _literal_url(value: str, *, allow_template_path: bool = False) -> LiteralHttpUrl | None:
    """Keep ownership bases literal while permitting templates in outgoing paths."""
    if ("{" in value and not allow_template_path) or value.startswith(
        ("env:", "setting:", "attr:")
    ):
        return None
    try:
        parsed = urlsplit(value if "://" in value or value.startswith("//") else "//" + value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"", "http", "https"}
        or "{" in parsed.netloc
        or not host
        or parsed.username is not None
    ):
        return None
    return LiteralHttpUrl(parsed.scheme, host, port, normalize_path(parsed.path))


def matching_prefix(base: LiteralHttpUrl, call: LiteralHttpUrl) -> str | None:
    if base.host != call.host or (base.scheme and call.scheme and base.scheme != call.scheme):
        return None
    scheme = call.scheme or base.scheme
    default = {"http": 80, "https": 443}.get(scheme)
    base_port = base.port if base.port is not None else default
    call_port = call.port if call.port is not None else default
    if base_port == call_port and (
        base.path == "/" or call.path == base.path or call.path.startswith(base.path + "/")
    ):
        return base.path
    return None


class EndpointCatalog:
    """Read-only candidates with scope applied before every matching algorithm."""

    def __init__(self, snapshot: GraphSnapshot) -> None:
        self._nodes = {
            key: node.model_copy(deep=True)
            for key, node in snapshot.nodes.items()
            if node.kind == NodeKind.API_ENDPOINT
        }
        self._endpoints = tuple(n for n in self._nodes.values() if n.kind == NodeKind.API_ENDPOINT)
        self.scopes = frozenset((n.repo_id, n.service_id) for n in self._endpoints)
        self._matchers: dict[frozenset[ServiceRef], _EndpointMatcher] = {}
        self._bases: list[tuple[LiteralHttpUrl, ServiceRef]] = []
        for repo, inputs in snapshot.link_inputs.items():
            for service in inputs.service_map.services:
                for base in service.base_urls:
                    literal = _literal_url(base)
                    if literal is not None:
                        self._bases.append((literal, (repo, service.id)))

    @classmethod
    def from_snapshot(cls, snapshot: GraphSnapshot) -> EndpointCatalog:
        return cls(snapshot)

    @property
    def endpoints(self) -> list[GraphNode]:
        return [node.model_copy(deep=True) for node in self._endpoints]

    def get_node(self, node_id: str) -> GraphNode | None:
        node = self._nodes.get(node_id)
        return node.model_copy(deep=True) if node is not None else None

    path_target = staticmethod(_EndpointMatcher.path_target)

    def _matcher(self, scopes: frozenset[ServiceRef]) -> _EndpointMatcher:
        cached = self._matchers.get(scopes)
        if cached is not None:
            return cached
        snap = GraphSnapshot()
        for node in self._endpoints:
            if (node.repo_id, node.service_id) in scopes:
                snap.add_node(node.model_copy(deep=True))
        matcher = _EndpointMatcher(snap)
        self._matchers[scopes] = matcher
        return matcher

    def by_method_path(
        self, method: str, path: str, *, scopes: frozenset[ServiceRef], owned: bool = False
    ) -> list[GraphNode]:
        return [
            node.model_copy(deep=True)
            for node in self._matcher(scopes).by_method_path(method, path, owned=owned)
        ]

    def by_operation(self, call_name: str, *, scopes: frozenset[ServiceRef]) -> list[GraphNode]:
        return [
            node.model_copy(deep=True) for node in self._matcher(scopes).by_operation(call_name)
        ]

    def literal_parameter(
        self, method: str, path: str, *, scopes: frozenset[ServiceRef], owned: bool = False
    ) -> list[tuple[GraphNode, dict[str, str]]]:
        return [
            (node.model_copy(deep=True), dict(bound))
            for node, bound in self._matcher(scopes).literal_parameter(method, path, owned=owned)
        ]

    def by_path_suffix(
        self, method: str, path: str, *, scopes: frozenset[ServiceRef]
    ) -> list[GraphNode]:
        return [
            node.model_copy(deep=True)
            for node in self._matcher(scopes).by_path_suffix(method, path)
        ]

    def literal_base_prefixes(self, url: str) -> BasePrefixes:
        if not ("://" in url or url.startswith("//")):
            return {}
        literal = _literal_url(url, allow_template_path=True)
        if literal is None:
            return {}
        matches = [
            (prefix, scope)
            for base, scope in self._bases
            if (prefix := matching_prefix(base, literal)) is not None
        ]
        longest = max((len(prefix) for prefix, _ in matches), default=-1)
        prefixes: BasePrefixes = defaultdict(set)
        for prefix, scope in matches:
            if len(prefix) == longest:
                prefixes[scope].add(prefix)
        return dict(prefixes)

    def literal_base_scopes(self, url: str) -> frozenset[ServiceRef]:
        return frozenset(self.literal_base_prefixes(url))


class EndpointIndex(EndpointCatalog):
    """Compatibility facade for legacy callers without explicit scopes."""

    def by_method_path(
        self,
        method: str,
        path: str,
        *,
        scopes: frozenset[ServiceRef] | None = None,
        owned: bool = False,
    ) -> list[GraphNode]:
        return super().by_method_path(
            method, path, scopes=self.scopes if scopes is None else scopes, owned=owned
        )

    def by_operation(
        self, call_name: str, service_id: str = "", *, scopes: frozenset[ServiceRef] | None = None
    ) -> list[GraphNode]:
        selected = self.scopes if scopes is None else scopes
        if service_id:
            selected = frozenset(s for s in selected if s[1] == service_id)
        return super().by_operation(call_name, scopes=selected)

    def literal_parameter(
        self,
        method: str,
        path: str,
        *,
        scopes: frozenset[ServiceRef] | None = None,
        owned: bool = False,
    ) -> list[tuple[GraphNode, dict[str, str]]]:
        return super().literal_parameter(
            method, path, scopes=self.scopes if scopes is None else scopes, owned=owned
        )

    def by_path_suffix(
        self, method: str, path: str, *, scopes: frozenset[ServiceRef] | None = None
    ) -> list[GraphNode]:
        return super().by_path_suffix(
            method, path, scopes=self.scopes if scopes is None else scopes
        )
