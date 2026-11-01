"""Isolated graph previews for configuration edits; never publish or cache."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from axon.v2.configedit import EditError, EditPlan
from axon.v2.graph_delta import GraphDelta


def preview_impact(root: Path, plan: EditPlan) -> dict[str, Any]:
    from axon.v2.indexer import index_repo
    from axon.v2.models import EdgeKind, NodeKind

    before, sm = index_repo(root, use_cache=False, workers=1)
    with tempfile.TemporaryDirectory(prefix="axon-preview-") as temporary:
        shadow = Path(temporary).resolve() / root.name
        shutil.copytree(
            root,
            shadow,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "node_modules",
                "__pycache__",
                ".worktrees",
                ".axon",
                "dist",
            ),
        )
        for name in plan.diffs:
            path = shadow / Path(name).relative_to(root)
            if not path.resolve().is_relative_to(shadow):
                raise EditError(f"preview config points outside shadow: {name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(plan.after[name])
        after, _ = index_repo(shadow, use_cache=False, workers=1)
        delta = GraphDelta.between(before, after)

        def outbound(snap: Any) -> dict[str, Any]:
            return {
                f"{e.from_id}|{e.to_id}|{e.evidence.rule}": e
                for e in snap.edges
                if e.kind == EdgeKind.CALLS_API
            }

        old, new = outbound(before), outbound(after)
        added = [e.model_dump(mode="json") for key, e in new.items() if key not in old]
        removed = [e.model_dump(mode="json") for key, e in old.items() if key not in new]
        newly_resolved = [e for e in added if after.nodes[e["to_id"]].kind == NodeKind.API_ENDPOINT]
        return {
            "repo": sm.repo,
            "graph_delta": {
                "nodes_added_or_changed": len(delta.upsert_nodes),
                "nodes_removed": sorted(delta.remove_node_ids),
                "outbound_added": added,
                "outbound_removed": removed,
            },
            "newly_resolved": newly_resolved,
            "coverage": after.coverage_by_service(sm.repo),
            "unresolved": after.unresolved,
            "resolution": after.resolution_report(sm.repo),
            "golden_proposals": [
                {"from_id": e["from_id"], "to_id": e["to_id"], "reviewed": False}
                for e in newly_resolved
            ],
        }
