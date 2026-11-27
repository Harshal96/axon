"""PR impact: changed files -> changed symbols -> affected APIs/services."""

from __future__ import annotations

import subprocess
from collections import deque
from pathlib import Path

from pydantic import BaseModel, Field

from axon.v1.graph import GraphSnapshot
from axon.v1.graphql_inputs import graphql_dependency_paths
from axon.v1.models import EdgeKind, NodeKind, endpoint_label, is_graphql


class ImpactResult(BaseModel):
    changed_files: list[str] = Field(default_factory=list)
    changed_symbols: list[str] = Field(default_factory=list)
    direct_apis: list[str] = Field(default_factory=list)
    affected_apis: list[str] = Field(default_factory=list)
    affected_services: list[str] = Field(default_factory=list)
    affected_repos: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


def git_changed_files(repo: str | Path, base: str, head: str = "HEAD") -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def compute_impact(
    snap: GraphSnapshot, changed_files: list[str], repo_id: str = ""
) -> ImpactResult:
    changed = set(changed_files)
    # Changed declarations, resolver registrations and executed documents may
    # live in different files from the caller and handler symbols themselves.
    symbols: list[str] = []
    direct: set[str] = set()
    for node in snap.nodes.values():
        if repo_id and node.repo_id and node.repo_id != repo_id:
            continue
        if node.file not in changed:
            continue
        if node.kind == NodeKind.SYMBOL:
            symbols.append(node.id)
    caller_symbols = set(symbols)
    for edge in snap.edges:
        if edge.kind not in (EdgeKind.DEFINES, EdgeKind.HANDLES, EdgeKind.CALLS_API):
            continue
        source = snap.nodes.get(edge.from_id)
        target = snap.nodes.get(edge.to_id)
        if source is None or target is None or (repo_id and source.repo_id != repo_id):
            continue
        if edge.kind == EdgeKind.DEFINES:
            if (
                source.kind == NodeKind.API_SPEC
                and target.kind == NodeKind.API_ENDPOINT
                and (source.file in changed or edge.evidence.file in changed)
            ):
                direct.add(target.id)
        elif is_graphql(target):
            if edge.kind == EdgeKind.HANDLES and (
                edge.evidence.file in changed or edge.props.get("registration_file") in changed
            ):
                direct.add(target.id)
            if edge.kind == EdgeKind.CALLS_API and (
                edge.evidence.file in changed
                or not changed.isdisjoint(graphql_dependency_paths(edge, client_only=True))
            ):
                caller_symbols.add(source.id)
    for sym_id in caller_symbols:
        for ep in snap.endpoint_of_symbol(sym_id):
            direct.add(ep.id)
    # Reverse reachability over the endpoint call graph.
    affected: set[str] = set(direct)
    queue: deque[str] = deque(direct)
    while queue:
        cur = queue.popleft()
        for _, caller_sym in snap.inbound_callers(cur):
            for owner in snap.endpoint_of_symbol(caller_sym.id):
                if owner.id not in affected:
                    affected.add(owner.id)
                    queue.append(owner.id)
    services, repos = set(), set()
    for ep_id in affected:
        ep_node = snap.nodes.get(ep_id)
        if ep_node is not None:
            if ep_node.service_id:
                services.add(f"{ep_node.repo_id}::{ep_node.service_id}")
            if ep_node.repo_id:
                repos.add(ep_node.repo_id)
    labels = []
    for ep_id in sorted(affected):
        n = snap.nodes.get(ep_id)
        labels.append(f"{endpoint_label(n)} ({n.service_id})" if n else ep_id)
    return ImpactResult(
        changed_files=sorted(changed),
        changed_symbols=sorted(symbols),
        direct_apis=sorted(direct),
        affected_apis=labels,
        affected_services=sorted(services),
        affected_repos=sorted(repos),
        unresolved=list(snap.unresolved[:20]),
    )


def render_markdown(result: ImpactResult) -> str:
    lines = ["## Axon API impact", ""]
    if not result.affected_apis:
        lines.append("No indexed APIs are affected by this change.")
    else:
        lines.append(
            f"**{len(result.affected_apis)} affected API(s)** "
            f"in services: {', '.join(result.affected_services) or '—'}"
        )
        lines.append("")
        lines.extend(f"- `{api}`" for api in result.affected_apis)
    if result.changed_files:
        lines += [
            "",
            f"Changed files: {len(result.changed_files)} ({len(result.changed_symbols)} symbols).",
        ]
    if result.unresolved:
        lines += [
            "",
            f"Unresolved outbound calls (need axon.yaml review): {len(result.unresolved)}.",
        ]
    lines += ["", "_Static analysis only: external/runtime callers are not visible._"]
    return "\n".join(lines) + "\n"
