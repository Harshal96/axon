"""PR impact: changed files -> changed symbols -> affected APIs/services."""

from __future__ import annotations

import subprocess
from collections import deque
from pathlib import Path

from pydantic import BaseModel, Field

from axon.v1.graph import GraphSnapshot
from axon.v1.models import EdgeKind, NodeKind, repo_node_id


class ImpactResult(BaseModel):
    publication_id: str = ""
    indexed_commit: str = ""
    affected_endpoint_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(
        default_factory=lambda: [
            "Indexed graph only; no baseline comparison is performed.",
            "Deleted endpoints absent from the indexed graph cannot be covered without a baseline.",
            "Changed paths alone do not establish checkout freshness or the current revision.",
            "External callers and runtime-only dispatch are unknown.",
        ]
    )
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
    repo = snap.nodes.get(repo_node_id(repo_id)) if repo_id else None
    if repo_id and repo is None:
        raise ValueError(f"unknown repository: {repo_id}")
    changed = sorted(set(changed_files))
    # Symbols defined in changed files (+ endpoints from changed specs).
    symbols: list[str] = []
    direct: set[str] = set()
    for node in snap.nodes.values():
        if repo_id and node.repo_id != repo_id:
            continue
        if node.file not in changed:
            continue
        if node.kind == NodeKind.SYMBOL:
            symbols.append(node.id)
        elif node.kind == NodeKind.API_SPEC:
            for e in snap.edges:
                if e.from_id == node.id and e.to_id in snap.nodes:
                    direct.add(e.to_id)
    for sym_id in symbols:
        for ep in snap.endpoint_of_symbol(sym_id):
            direct.add(ep.id)
    # Reverse reachability over the endpoint call graph.
    affected: set[str] = set(direct)
    traversed_pinned_repos: set[str] = set()
    queue: deque[str] = deque(direct)
    while queue:
        cur = queue.popleft()
        for edge, caller_sym in snap.inbound_callers(cur):
            if caller_sym.repo_id and (
                caller_sym.repo_id not in snap.link_inputs
                or edge.props.get("relink_status") == "pinned-legacy"
            ):
                traversed_pinned_repos.add(caller_sym.repo_id)
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
        labels.append(
            f"{n.props.get('method', '')} {n.props.get('path', '')} ({n.repo_id}::{n.service_id})"
            if n
            else ep_id
        )
    limitations = list(ImpactResult().limitations)
    # The walk proves only participation visible in retained rows. Missing inputs
    # or historical pins elsewhere can also hide callers; they are not affected
    # repo evidence and must not expand affected_repos/affected_endpoint_ids.
    missing_inputs = {
        node.repo_id for node in snap.by_kind(NodeKind.REPO)
    } - snap.link_inputs.keys()
    pinned_repos = {
        snap.nodes[edge.from_id].repo_id
        for edge in snap.edges
        if edge.kind == EdgeKind.CALLS_API
        and edge.from_id in snap.nodes
        and edge.props.get("relink_status") == "pinned-legacy"
    }
    for scope in sorted(traversed_pinned_repos):
        limitations.append(
            f"Known impact traversal uses pinned-legacy evidence from {scope}; "
            f"fresh matching is unknown. Re-index {scope}."
        )
    for scope in sorted(missing_inputs | pinned_repos):
        cause = (
            "retained link inputs missing"
            if scope in missing_inputs
            else "pinned-legacy calls remain"
        )
        limitations.append(
            f"{scope}: {cause}; fresh matching and caller coverage, including "
            f"possibly omitted callers, are unknown. Re-index {scope}. "
            "This does not establish participation in the change."
        )
    return ImpactResult(
        limitations=limitations,
        publication_id=snap.publication_id,
        indexed_commit=repo.props.get("commit", "") if repo else "",
        affected_endpoint_ids=sorted(affected),
        changed_files=changed,
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
    lines += ["", *[f"_Limit: {limit}_" for limit in result.limitations]]
    return "\n".join(lines) + "\n"
