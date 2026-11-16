"""Pure caller-input preparation and shared-catalog outbound reconciliation."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass

from axon.v1.endpoint_catalog import EndpointCatalog, _operation_base
from axon.v1.facts import FileFacts, HttpFact, normalize_path, route_specificity
from axon.v1.graph import GraphSnapshot
from axon.v1.link_inputs import PinnedCallInput, PreparedCallInput, RepoLinkInputs, RepoLinkReport
from axon.v1.linker import (
    _base_matches,
    base_reference,
    operation_candidates,
    reconcile_path_fallbacks,
    resolve_http,
    url_path,
)
from axon.v1.manual_corrections import add_manual_calls, suppress_edges
from axon.v1.models import (
    Confidence,
    EdgeKind,
    Evidence,
    GraphEdge,
    GraphNode,
    NodeKind,
    endpoint_node_id,
    external_node_id,
    symbol_node_id,
)
from axon.v1.sdk_catalog import url_shaped
from axon.v1.servicemap import ServiceMap, _matches, owner_of


@dataclass
class RepoLinkResult:
    edges: list[GraphEdge]
    placeholders: dict[str, GraphNode]
    report: RepoLinkReport


def prepare_link_inputs(
    repo_snapshot: GraphSnapshot,
    service_map: ServiceMap,
    resolved_files: list[FileFacts],
    manual_sources: dict[str, str],
    *,
    repo_id: str | None = None,
) -> RepoLinkInputs:
    """Detach parser facts before any catalog-dependent filtering or binding."""
    repos = {node.repo_id for node in repo_snapshot.nodes.values() if node.kind == NodeKind.REPO}
    if repo_id is None and len(repos) != 1:
        raise ValueError("prepare_link_inputs requires exactly one repository")
    if repo_id is not None and repos and repo_id not in repos:
        raise ValueError(f"source repository {repo_id} is absent from snapshot")
    repo = repo_id if repo_id is not None else next(iter(repos))
    inputs = RepoLinkInputs(
        repo,
        deepcopy(service_map),
        source_commit=repo_snapshot.commit,
        manual_sources=dict(manual_sources),
    )
    for facts in resolved_files:
        kind, owner = owner_of(service_map, facts.path)
        if kind == "client":
            inputs.static_suppression_counts[owner] = (
                inputs.static_suppression_counts.get(owner, 0) + facts.suppressed_calls
            )
        precise = {(h.caller, h.line) for h in facts.http_calls if h.via == "call-through"}
        suppressed = {d.line for d in facts.diagnostics if d.category == "Suppressed"}
        calls = {(c.caller, c.line): c for c in facts.calls}
        for raw in facts.http_calls:
            fact = deepcopy(raw)
            reason = ""
            if kind == "client" and not (
                fact.url and url_shaped(fact.url) and fact.via not in {"call-through", "sdk"}
            ):
                reason = "client-source"
            elif fact.via == "generated-client" and (
                (fact.caller, fact.line) in precise or fact.line in suppressed
            ):
                reason = "precise-or-suppressed"
            caller_id = symbol_node_id(repo, facts.path, fact.caller)
            caller = repo_snapshot.nodes.get(caller_id)
            if caller is not None and caller.repo_id and caller.repo_id != repo:
                continue
            if caller_id not in repo_snapshot.nodes and not reason:
                continue
            if fact.via == "generated-source":
                fact.callee = fact.callee or f"{facts.path}::{fact.caller}"
                if not fact.client_id and kind == "client":
                    identities = [
                        (svc.id, c.id)
                        for svc in service_map.services
                        for c in svc.clients
                        if any(_matches(facts.path, path) for path in c.paths)
                    ]
                    if len(identities) == 1:
                        fact.client_owner, fact.client_id = identities[0]
            call = calls.get((fact.caller, fact.line))
            count = (
                call.argument_count
                if call and call.argument_count >= 0
                else len(call.arguments)
                if call
                else 0
            )
            inputs.prepared.append(
                PreparedCallInput(caller_id, facts.path, kind, owner, count, reason, fact)
            )
    return inputs


def resolve_repo_links(
    structure: GraphSnapshot, inputs: RepoLinkInputs, catalog: EndpointCatalog
) -> RepoLinkResult:
    """Replay one caller without source IO or mutations of published objects."""
    if inputs.format_version != 1:
        raise ValueError(
            f"unsupported link inputs format {inputs.format_version} for {inputs.repo_id}; "
            "re-index repository"
        )
    repo_id, sm, sources, index = inputs.repo_id, inputs.service_map, inputs.manual_sources, catalog
    # Local structure only; API targets are looked up through the immutable catalog.
    pinned_targets = {pin.expected_target_id for pin in inputs.pinned}
    prepared_callers = {item.caller_id for item in inputs.prepared}
    snap = GraphSnapshot(
        nodes={
            key: node.model_copy(deep=True)
            for key, node in structure.nodes.items()
            if (node.repo_id == repo_id and node.kind != NodeKind.EXTERNAL_API)
            or (not node.repo_id and key in prepared_callers and node.kind == NodeKind.SYMBOL)
            or (node.kind == NodeKind.EXTERNAL_API and key in pinned_targets)
        },
        edges=[
            edge.model_copy(deep=True)
            for edge in structure.edges
            if edge.kind != EdgeKind.CALLS_API
            and edge.from_id in structure.nodes
            and structure.nodes[edge.from_id].repo_id == repo_id
        ],
    )
    # Legacy private-linker callers may omit Repo rows and symbol ownership.
    # Only prepared symbol identities qualify; never adopt foreign-owned rows.
    for caller_id in prepared_callers:
        caller = snap.nodes.get(caller_id)
        if caller is not None and not caller.repo_id:
            caller.repo_id = repo_id
    first_edge = len(snap.edges)
    source_suppressed = dict(inputs.static_suppression_counts)
    accessor_suppressed: dict[str, int] = {}
    for item in inputs.prepared:
        if item.static_skip_reason:
            if item.static_skip_reason == "client-source":
                source_suppressed[item.owner_id] = source_suppressed.get(item.owner_id, 0) + 1
            continue
        fact = deepcopy(item.fact)
        caller_id = item.caller_id
        if caller_id not in snap.nodes:
            continue
        if fact.via == "sdk":
            target = external_node_id(
                "SDK", fact.provider + ":" + fact.call_name.rsplit(".", 1)[-1]
            )
            snap.add_node(
                GraphNode(
                    id=target,
                    kind=NodeKind.EXTERNAL_API,
                    name=fact.call_name.rsplit(".", 1)[-1],
                    props={
                        "kind": "sdk",
                        "provider": fact.provider,
                        "operation": fact.call_name,
                    },
                )
            )
            snap.add_edge(
                GraphEdge(
                    from_id=caller_id,
                    to_id=target,
                    kind=EdgeKind.CALLS_API,
                    evidence=Evidence(file=item.file, line=fact.line, rule="external:sdk"),
                    props={"kind": "sdk", "provider": fact.provider},
                )
            )
            continue
        if fact.url and not url_shaped(fact.url):
            snap.diagnostics.append(f"{item.file}:{fact.line}: suppressed: non-url literal")
            continue
        if fact.via in {"call-through", "generated-client"} and not fact.url:
            leaf = fact.call_name.rsplit(".", 1)[-1]
            count = item.argument_count
            accessor = bool(re.fullmatch(r"(?:get|is|has)[A-Z]\w*", leaf)) and count == 0
            accessor |= bool(re.fullmatch(r"set[A-Z]\w*", leaf)) and count == 1
            accessor |= bool(
                re.fullmatch(
                    r"builder|build|with[A-Z]\w*|getInstance|toString|hashCode|equals|getClass|wait|notify|notifyAll",
                    leaf,
                )
            )
            if accessor and not operation_candidates(fact, index, sm, repo_id):
                accessor_suppressed[item.owner_id] = accessor_suppressed.get(item.owner_id, 0) + 1
                continue
        resolved = resolve_http(fact, index, sm, repo_id, snap.nodes[caller_id].service_id, sources)
        if resolved is None:
            template = url_path(fact.url) or fact.call_name or "unknown"
            base_ref = base_reference(fact.url)
            # Preserve symbolic ownership evidence and distinct literal hosts.
            if base_ref:
                template = fact.url.split("?")[0].split("#")[0]
            dynamic = bool(fact.url) and route_specificity(url_path(fact.url))[0] == 0
            candidates = (
                []
                if dynamic
                else index.by_method_path(
                    fact.method or "ANY", url_path(fact.url), scopes=index.scopes
                )
            )
            if (
                not dynamic
                and not candidates
                and base_ref.startswith(("env:", "setting:", "attr:"))
                and sm.index.path_suffix_fallback
            ):
                candidates = index.by_path_suffix(
                    fact.method, url_path(fact.url), scopes=index.scopes
                )
            path_ambiguous = len({(h.repo_id, h.service_id) for h in candidates}) > 1
            operations = (
                index.by_operation(fact.call_name, scopes=index.scopes)
                if fact.call_name and not path_ambiguous
                else []
            )
            operation_ambiguous = len(operations) > 1
            dedupe_suffix = operation_ambiguous and bool(
                re.search(r"_\d+$", _operation_base(fact.call_name))
            )
            candidate_ids = sorted({h.id for h in [*candidates, *operations]})
            ext_id = external_node_id(fact.method or "ANY", template)
            snap.add_node(
                GraphNode(
                    id=ext_id,
                    kind=NodeKind.EXTERNAL_API,
                    name=f"{fact.method or 'ANY'} {template}".strip(),
                    props={
                        "method": fact.method,
                        "url": fact.url,
                        "call_name": fact.call_name,
                        "base_ref": base_ref,
                        "candidates": json.dumps(candidate_ids),
                    },
                )
            )
            snap.add_edge(
                GraphEdge(
                    from_id=caller_id,
                    to_id=ext_id,
                    kind=EdgeKind.CALLS_API,
                    confidence=Confidence.AMBIGUOUS,
                    evidence=Evidence(
                        file=item.file,
                        line=fact.line,
                        rule="unresolved:dynamic-path"
                        if dynamic
                        else "unresolved:path-ambiguous"
                        if path_ambiguous
                        else f"client:{fact.client_id}(dedupe-suffix)"
                        if dedupe_suffix and fact.client_id
                        else "unresolved:operation-ambiguous"
                        if operation_ambiguous
                        else f"unresolved:{fact.via}",
                        detail=fact.detail or fact.call_name,
                    ),
                    props={
                        "method": fact.method,
                        "url": fact.url,
                        "call_name": fact.call_name,
                        "via": fact.via,
                        "binding_rule": fact.binding_rule,
                        "client_id": fact.client_id,
                        "base_ref": base_ref,
                        "candidates": json.dumps(candidate_ids),
                        **_target_scope(fact, inputs, catalog),
                    },
                )
            )
            snap.unresolved.append(
                f"unresolved outbound call {item.file}:{fact.line} "
                f"{fact.call_name} {fact.url}".strip()
            )
        else:
            target_id, conf, rule = resolved
            expected_scope = {}
            if fact.ambiguous and conf != Confidence.MANUAL:
                conf = Confidence.AMBIGUOUS
                rule += "(many-binding)"
            if index.get_node(target_id) is None:
                expected_scope = next(
                    (
                        {"target_repo": link.to_repo or repo_id, "target_service": link.to_service}
                        for link in [
                            *sm.links,
                            *(link for svc in sm.services for link in svc.links),
                        ]
                        if endpoint_node_id(
                            link.to_repo or repo_id,
                            link.to_service,
                            link.to_method,
                            normalize_path(link.to_path),
                        )
                        == target_id
                    ),
                    {},
                )
                snap.unresolved.append(
                    f"manual link target not indexed yet: {target_id} "
                    f"(from {item.file}:{fact.line})"
                )
                if conf != Confidence.MANUAL:
                    continue
                template = target_id
                target_id = external_node_id(fact.method or "ANY", template)
                snap.add_node(
                    GraphNode(
                        id=target_id,
                        kind=NodeKind.EXTERNAL_API,
                        name=template,
                        props={"target_endpoint": template, "call_name": fact.call_name},
                    )
                )
            snap.add_edge(
                GraphEdge(
                    from_id=caller_id,
                    to_id=target_id,
                    kind=EdgeKind.CALLS_API,
                    confidence=conf,
                    evidence=Evidence(
                        file=item.file,
                        line=fact.line,
                        rule=rule,
                        detail=fact.detail or fact.call_name,
                    ),
                    props={
                        "method": fact.method,
                        "url": fact.url,
                        "call_name": fact.call_name,
                        "via": fact.via,
                        "binding_rule": fact.binding_rule,
                        "client_id": fact.client_id,
                        "callee": fact.callee,
                        **expected_scope,
                        **(
                            {
                                "source_file": rule[7:].split("#")[0],
                                "entry_id": rule.partition("#")[2],
                            }
                            if conf == Confidence.MANUAL
                            else {}
                        ),
                        "base_ref": fact.base_ref or base_reference(fact.url),
                        **({"bound": json.dumps(fact.bound)} if fact.bound else {}),
                    },
                )
            )
    for service, count in sorted(accessor_suppressed.items()):
        snap.diagnostics.append(f"{service}: suppressed: non-operation model accessor: {count}")
    for service, count in sorted(source_suppressed.items()):
        snap.diagnostics.append(f"{service}: client-source calls suppressed: {count}")
    fresh_edges = snap.edges[first_edge:]
    # Reference pinned externals before consensus prunes unused placeholders.
    # Only newly resolved sites may vote in or receive fresh consensus inference.
    _replay_pinned(snap, inputs, catalog)
    reconcile_path_fallbacks(snap, fresh_edges, sm, catalog=index)
    add_manual_calls(snap, repo_id, sm, sources, catalog=catalog)
    suppress_edges(snap, repo_id, sm, catalog=catalog)
    snap.dedupe_edges()
    edges = [edge for edge in snap.edges if edge.kind == EdgeKind.CALLS_API]
    for edge in edges:
        edge.props.setdefault("owner_file", edge.evidence.file or snap.nodes[edge.from_id].file)
    # Derive unresolved output from surviving edges, after consensus/suppression.
    snap.unresolved = _unresolved_messages(snap, edges)
    # A temporary reporting view contains targets, never the caller's structure.
    report_view = GraphSnapshot(nodes=dict(snap.nodes), edges=list(snap.edges))
    for edge in edges:
        if endpoint := catalog.get_node(edge.to_id):
            report_view.nodes[endpoint.id] = endpoint
    return RepoLinkResult(
        edges,
        {key: node for key, node in snap.nodes.items() if node.kind == NodeKind.EXTERNAL_API},
        RepoLinkReport(
            list(snap.unresolved), list(snap.diagnostics), report_view.coverage_by_service(repo_id)
        ),
    )


def _target_scope(
    fact: HttpFact, inputs: RepoLinkInputs, catalog: EndpointCatalog
) -> dict[str, str]:
    """Caller-specific expected ownership belongs to the edge, not shared externals."""
    repo = inputs.repo_id
    for service in inputs.service_map.services:
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
            if fact.call_name and (identity or package):
                return (
                    {
                        "target_repo": client.target_repo or repo,
                        "target_service": client.target_service,
                    }
                    if client.target_service
                    else {}
                )
    ref = base_reference(fact.url)
    scopes = {
        (client.target_repo or repo, client.target_service)
        for service in inputs.service_map.services
        for client in service.clients
        if client.target_service
        and any(_base_matches(base, fact.url, ref) for base in client.base_urls)
    }
    if not scopes:
        scopes = set(catalog.literal_base_scopes(fact.url))
    if not scopes:
        scopes = {
            (repo, service.id)
            for service in inputs.service_map.services
            if any(_base_matches(base, fact.url, ref) for base in service.base_urls)
        }
    if len(scopes) != 1:
        return {}
    target_repo, target_service = next(iter(scopes))
    return {"target_repo": target_repo, "target_service": target_service}


def _unresolved_messages(snap: GraphSnapshot, edges: list[GraphEdge]) -> list[str]:
    messages = []
    for edge in edges:
        node = snap.nodes.get(edge.to_id)
        if node is None or node.kind != NodeKind.EXTERNAL_API or node.props.get("kind") == "sdk":
            continue
        entry_id = edge.props.get("entry_id", "")
        expected = node.props.get("target_endpoint", "")
        if expected and entry_id:
            message = (
                f"manual link target not indexed yet: {expected} "
                f"(from {edge.evidence.file}:{edge.evidence.line})"
            )
        elif expected:
            message = f"pinned link target not indexed: {expected}"
        elif entry_id and node.props.get("target_service"):
            owner = snap.nodes[edge.from_id].service_id
            template = (
                f"{node.props.get('target_repo', '')}/{node.props['target_service']}:"
                + node.props.get("path", "")
            )
            message = f"declared call target not indexed: {owner}/{entry_id}: {template}"
        else:
            message = (
                f"unresolved outbound call {edge.evidence.file}:{edge.evidence.line} "
                f"{edge.props.get('call_name', '')} {edge.props.get('url', '')}"
            ).strip()
        messages.append(message)
    return list(dict.fromkeys(messages))


def _replay_pinned(snap: GraphSnapshot, inputs: RepoLinkInputs, catalog: EndpointCatalog) -> None:
    if inputs.pinned:
        snap.diagnostics.append(
            f"{inputs.repo_id}: link-inputs-missing: legacy outbound targets pinned; "
            "re-index repository"
        )
    for pin in inputs.pinned:
        if pin.edge.from_id not in snap.nodes:
            continue
        edge = pin.edge.model_copy(deep=True)
        edge.props["relink_status"] = "pinned-legacy"
        edge.to_id = pin.expected_target_id
        if (
            edge.to_id not in snap.nodes
            and pin.external_target is not None
            and pin.external_target.kind == NodeKind.EXTERNAL_API
            and pin.external_target.id == edge.to_id
        ):
            snap.add_node(pin.external_target.model_copy(deep=True))
        if catalog.get_node(edge.to_id) is None and edge.to_id not in snap.nodes:
            edge.to_id = external_node_id(pin.target_method or "ANY", pin.expected_target_id)
            snap.add_node(
                GraphNode(
                    id=edge.to_id,
                    kind=NodeKind.EXTERNAL_API,
                    name=pin.target_name or pin.expected_target_id,
                    props={
                        "target_endpoint": pin.expected_target_id,
                        "target_repo": pin.target_repo,
                        "target_service": pin.target_service,
                        "method": pin.target_method,
                        "path": pin.target_path,
                    },
                )
            )
            snap.unresolved.append(f"pinned link target not indexed: {pin.expected_target_id}")
        snap.add_edge(edge)


def _legacy_inputs(snapshot: GraphSnapshot, repo: str) -> RepoLinkInputs:
    inputs = RepoLinkInputs(repo, ServiceMap(repo=repo))
    for edge in snapshot.edges:
        caller = snapshot.nodes.get(edge.from_id)
        if edge.kind != EdgeKind.CALLS_API or caller is None or caller.repo_id != repo:
            continue
        target = snapshot.nodes.get(edge.to_id)
        inputs.pinned.append(
            PinnedCallInput(
                edge.model_copy(deep=True),
                edge.to_id,
                target.name if target else "",
                target.repo_id if target else "",
                target.service_id if target else "",
                target.props.get("method", "") if target else "",
                target.props.get("path", "") if target else "",
                target.model_copy(deep=True)
                if target and target.kind == NodeKind.EXTERNAL_API
                else None,
            )
        )
    return inputs


def reconcile_repositories(
    current: GraphSnapshot,
    replacements: GraphSnapshot,
    *,
    replace_repo_ids: set[str],
    remove_repo_ids: frozenset[str] = frozenset(),
) -> GraphSnapshot:
    """Build the prospective catalog, then replay every retained caller's inputs."""
    dropped = replace_repo_ids | remove_repo_ids
    nodes = {key: node for key, node in current.nodes.items() if node.repo_id not in dropped}
    nodes.update(
        {
            key: node
            for key, node in replacements.nodes.items()
            if node.repo_id in replace_repo_ids or node.kind == NodeKind.EXTERNAL_API
        }
    )
    edges = [
        edge
        for edge in current.edges
        if edge.kind != EdgeKind.CALLS_API
        and edge.from_id in nodes
        and edge.to_id in nodes
        and current.nodes[edge.from_id].repo_id not in dropped
    ]
    edges.extend(
        edge
        for edge in replacements.edges
        if edge.kind != EdgeKind.CALLS_API
        and edge.from_id in nodes
        and edge.to_id in nodes
        and replacements.nodes[edge.from_id].repo_id in replace_repo_ids
    )
    repos = {node.repo_id for node in nodes.values() if node.kind == NodeKind.REPO}
    inputs = {}
    for repo in sorted(repos):
        source = replacements if repo in replace_repo_ids else current
        inputs[repo] = (
            deepcopy(source.link_inputs[repo])
            if repo in source.link_inputs
            else _legacy_inputs(source, repo)
        )
    # Keep structural diagnostics; recompute all previous outbound diagnostics.
    old_unresolved = {
        line for report in current.link_reports.values() for line in report.unresolved
    }
    # Legacy snapshots have outbound lines without report containers. Remove
    # those prior lines before replay, keeping new aggregate entries per repo.
    old_unresolved.update(
        _unresolved_messages(
            current,
            [
                edge
                for edge in current.edges
                if edge.kind == EdgeKind.CALLS_API
                and (caller := current.nodes.get(edge.from_id)) is not None
                and caller.repo_id not in current.link_reports
            ],
        )
    )
    old_diagnostics = {
        line for report in current.link_reports.values() for line in report.diagnostics
    }
    new_unresolved = {
        line for report in replacements.link_reports.values() for line in report.unresolved
    }
    new_unresolved.update(
        _unresolved_messages(
            replacements,
            [
                edge
                for edge in replacements.edges
                if edge.kind == EdgeKind.CALLS_API
                and (caller := replacements.nodes.get(edge.from_id)) is not None
                and caller.repo_id not in replacements.link_reports
            ],
        )
    )
    new_diagnostics = {
        line for report in replacements.link_reports.values() for line in report.diagnostics
    }
    snap = GraphSnapshot(
        nodes=nodes,
        edges=edges,
        link_inputs=inputs,
        unresolved=list(
            dict.fromkeys(
                [line for line in current.unresolved if line not in old_unresolved]
                + [line for line in replacements.unresolved if line not in new_unresolved]
            )
        ),
        diagnostics=list(
            dict.fromkeys(
                [line for line in current.diagnostics if line not in old_diagnostics]
                + [line for line in replacements.diagnostics if line not in new_diagnostics]
            )
        ),
        commit=replacements.commit or current.commit,
        publication_id=replacements.publication_id or current.publication_id,
    )
    catalog = EndpointCatalog.from_snapshot(snap)
    for repo, retained in inputs.items():
        result = resolve_repo_links(snap, retained, catalog)
        # Updating containers avoids GraphSnapshot.add_node's in-place prop merge.
        snap.nodes.update(result.placeholders)
        snap.edges.extend(result.edges)
        snap.link_reports[repo] = result.report
        snap.unresolved.extend(result.report.unresolved)
        snap.diagnostics.extend(result.report.diagnostics)
    snap.prune_orphaned_externals()
    return snap
