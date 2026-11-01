"""Fresh-parse selection and focused reporting; publication remains repository-wide."""

from pathlib import Path
from typing import Any

from axon.v1.graph import GraphSnapshot
from axon.v1.graph_delta import GraphDelta, edge_key
from axon.v1.models import EdgeKind
from axon.v1.servicemap import ServiceMap, owner_of


def scoped_paths(root: Path, sm: ServiceMap, services: list[str]) -> set[str]:
    from axon.v1.indexer import collect_files

    unknown = set(services) - {service.id for service in sm.services}
    if unknown:
        raise ValueError("unknown services: " + ", ".join(sorted(unknown)))
    selected = set(services)
    if not selected:
        return set()
    return {path for path in collect_files(root, sm) if owner_of(sm, path)[1] in selected}


def focused_result(
    before: GraphSnapshot, after: GraphSnapshot, repo: str, services: list[str]
) -> dict[str, Any]:
    selected = set(services)

    def outbound(snapshot: GraphSnapshot) -> GraphSnapshot:
        return GraphSnapshot(
            edges=[
                edge
                for edge in snapshot.edges
                if edge.kind == EdgeKind.CALLS_API
                and (node := snapshot.nodes.get(edge.from_id)) is not None
                and node.repo_id == repo
                and node.service_id in selected
            ]
        )

    old = outbound(before)
    delta = GraphDelta.between(old, outbound(after))
    return {
        "services": list(dict.fromkeys(services)),
        "coverage": {
            key: value for key, value in after.coverage_by_service(repo).items() if key in selected
        },
        "resolution": {
            key: value for key, value in after.resolution_report(repo).items() if key in selected
        },
        "graph_delta": {
            "added": [edge.model_dump(mode="json") for edge in delta.add_edges],
            "removed": [
                edge.model_dump(mode="json")
                for edge in old.edges
                if edge_key(edge) in delta.remove_edge_keys
            ],
        },
    }


def evict_scoped_facts(root: Path, paths: set[str]) -> None:
    """Remove content-addressed entries before a guaranteed fresh parse."""
    from axon.v1.factscache import _entry_path, cache_dir, cache_key
    from axon.v1.indexer import PARSER_VERSIONS

    for path in paths:
        suffix = Path(path).suffix
        if suffix not in PARSER_VERSIONS:
            continue
        try:
            key = cache_key(suffix, PARSER_VERSIONS[suffix], (root / path).read_bytes())
            _entry_path(key, cache_dir()).unlink(missing_ok=True)
        except OSError:
            # Fresh tasks bypass the cache even when removing an entry fails.
            continue
