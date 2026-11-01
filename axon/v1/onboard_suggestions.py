"""Advisory URL scopes from source-visible paths; never changes ownership."""

import re
from collections import defaultdict
from pathlib import Path

from axon.v1.facts import FileFacts
from axon.v1.graph import GraphSnapshot
from axon.v1.linker import EndpointIndex, url_path
from axon.v1.models import GraphNode, NodeKind, endpoint_node_id
from axon.v1.parse_go import parse_go
from axon.v1.parse_java import parse_java
from axon.v1.parse_python import parse_python
from axon.v1.servicemap import ServiceMap, owner_of
from axon.v1.swagger import parse_swagger_file


def path_base_url_suggestions(root: Path, sm: ServiceMap, files: list[str]) -> list[str]:
    """Unique verb/path matches suggest a scope; disagreement remains visible."""
    snap = GraphSnapshot()
    parsed: list[tuple[str, FileFacts]] = []
    parsers = {".py": parse_python, ".java": parse_java, ".go": parse_go}

    def endpoint(service: str, method: str, path: str, base_path: str = "") -> None:
        identity = endpoint_node_id(sm.repo, service, method, path)
        snap.add_node(
            GraphNode(
                id=identity,
                kind=NodeKind.API_ENDPOINT,
                service_id=service,
                props={"method": method, "path": path, "base_path": base_path},
            )
        )

    for rel in files:
        kind, sid = owner_of(sm, rel)
        parser = parsers.get(Path(rel).suffix)
        if kind != "service" or parser is None:
            continue
        try:
            facts = parser(rel, (root / rel).read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        parsed.append((rel, facts))
        for route in facts.routes:
            endpoint(sid, route.method, route.path)
    for service in sm.services:
        for spec in service.specs:
            try:
                document = parse_swagger_file(spec, str(root))
            except (OSError, ValueError):
                continue
            if document:
                for spec_route in document.endpoints:
                    endpoint(service.id, spec_route.method, spec_route.path, document.base_path)
    index = EndpointIndex(snap)
    suggestions: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    declared = {ref for service in sm.services for ref in service.base_urls}
    declared.update(
        ref for service in sm.services for client in service.clients for ref in client.base_urls
    )
    for rel, facts in parsed:
        for call in facts.http_calls:
            match = re.match(r"^\{((?:env|setting|attr):[^}]+)\}", call.url)
            ref = call.base_ref or (match.group(1) if match else "")
            if not ref or ref in declared:
                continue
            hits = index.by_method_path(call.method, url_path(call.url))
            targets = {hit.service_id for hit in hits}
            if len(targets) == 1:
                suggestions[ref][targets.pop()].append(f"{rel}:{call.line}")
    notes = []
    for ref, target_sites in sorted(suggestions.items()):
        if len(target_sites) == 1:
            service_id, sites = next(iter(target_sites.items()))
            notes.append(
                f"Suggested {service_id} base_urls: [{ref}] from {len(sites)} call site(s): "
                + ", ".join(sites[:3])
                + "; review before declaring."
            )
        else:
            notes.append(
                f"Conflicting base URL {ref}: paths match services "
                f"{', '.join(sorted(target_sites))}; "
                "no scope suggested."
            )
    return notes


def client_factory_suggestions(root: Path, sm: ServiceMap, files: list[str]) -> list[str]:
    """Suggest undeclared settings factories for review; never edit configuration."""
    from axon.v1.onboard import _factory_matches

    suggestions: set[str] = set()
    services = {service.id: service for service in sm.services}
    for rel in files:
        kind, sid = owner_of(sm, rel)
        if kind != "service" or not rel.endswith(".py"):
            continue
        try:
            facts = parse_python(rel, (root / rel).read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        for call in facts.calls:
            head, dot, tail = call.called.partition(".")
            if facts.import_map.get(head) != "django.conf.settings" or not dot:
                continue
            factory = "settings." + tail
            for client in services[sid].clients:
                if factory in client.factories or tail in client.factories:
                    continue
                if any(
                    _factory_matches(factory, slug) for slug in [client.id, client.target_service]
                ):
                    suggestions.add(
                        f"{sid}: consider factories: [{factory}] on client {client.id} "
                        f"({rel}:{call.line}); review source-return inference first."
                    )
    return sorted(suggestions)
