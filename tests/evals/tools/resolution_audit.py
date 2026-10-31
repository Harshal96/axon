"""Audit a saved snapshot without database access or target-specific assumptions.

Run: uv run python -m tests.evals.tools.resolution_audit snapshot.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from axon.v2.graph import GraphSnapshot
from axon.v2.models import EdgeKind, NodeKind


def audit(snapshot: GraphSnapshot) -> dict[str, object]:
    buckets: Counter[str] = Counter()
    for edge in snapshot.edges:
        if edge.kind != EdgeKind.CALLS_API:
            continue
        rule = edge.evidence.rule
        target = snapshot.nodes[edge.to_id]
        leaf = edge.props.get("call_name", target.props.get("call_name", "")).rsplit(".", 1)[-1]
        if rule.startswith("client:"):
            bucket = "resolved client:*"
        elif rule == "match:method+path":
            bucket = rule
        elif "(many)" in rule or "(many-binding)" in rule:
            bucket = "match:*(many)"
        elif rule.startswith("unresolved:") and leaf[:1].isupper():
            bucket = "unresolved:constructor"
        elif (
            rule.startswith("unresolved:")
            and target.props.get("call_name", "").startswith("requests.")
            and not target.props.get("url")
        ):
            bucket = "unresolved:requests-template"
        elif leaf in {"toLowerCase", "getId", "isEmpty", "getOrDefault"}:
            bucket = "java:non-operation-leaf"
        else:
            bucket = rule or "unknown"
        buckets[bucket] += 1
    likely_missing = []
    for endpoint in snapshot.by_kind(NodeKind.API_ENDPOINT):
        if snapshot.outbound_calls(endpoint.id):
            continue
        handlers = [
            snapshot.nodes[e.from_id]
            for e in snapshot.edges
            if e.kind == EdgeKind.HANDLES and e.to_id == endpoint.id
        ]
        closure = {node for handler in handlers for node in snapshot.call_closure(handler.id)}
        files = {snapshot.nodes[node].file for node in closure}
        imports = [
            node.props.get("imports", "")
            for node in snapshot.by_kind(NodeKind.SOURCE_FILE)
            if node.file in files
        ]
        if any("requests" in value or "cli" in value for value in imports):
            likely_missing.append(endpoint.id)
    return {
        "calls_api_by_rule": dict(sorted(buckets.items())),
        "endpoints_without_outbound": sum(
            not snapshot.outbound_calls(e.id) for e in snapshot.by_kind(NodeKind.API_ENDPOINT)
        ),
        "likely_missing": likely_missing,
        "diagnostics": len(snapshot.diagnostics),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = (
        json.dumps(audit(GraphSnapshot.model_validate_json(args.snapshot.read_text())), indent=2)
        + "\n"
    )
    if args.out:
        args.out.write_text(result)
    else:
        print(result, end="")


if __name__ == "__main__":
    main()
