"""Build several source snapshots and publish them as a single catalog update."""

from contextlib import ExitStack
from pathlib import Path

from axon.v1.golden import validate_golden
from axon.v1.graph import GraphSnapshot
from axon.v1.graph_delta import lost_cross_repo_links
from axon.v1.incremental import SourcePathConflict, check_source_identity
from axon.v1.indexer import index_repo
from axon.v1.jobs import JOBS
from axon.v1.models import NodeKind
from axon.v1.revision import checkout_commit, checkout_dirty
from axon.v1.servicemap import load_service_map, service_map_fingerprint
from axon.v1.store import GraphStore, Neo4jStore


def publish_batch(
    store: GraphStore, paths: list[str]
) -> tuple[GraphSnapshot, list[str], list[dict[str, str | int]]]:
    roots: dict[str, Path] = {}
    for path in paths:
        root = Path(path).resolve()
        if not root.is_dir():
            raise ValueError(f"checkout is not a directory: {root}")
        repo = load_service_map(root).repo
        if repo in roots or root in roots.values():
            raise SourcePathConflict(f"duplicate checkout or repository {repo!r} in the batch")
        roots[repo] = root

    replacement = GraphSnapshot()
    observations: dict[str, tuple[str, bool | None, str]] = {}
    link_losses: list[dict[str, str | int]] = []

    def observe(root: Path) -> tuple[str, bool | None, str]:
        return checkout_commit(root), checkout_dirty(root), service_map_fingerprint(root)

    def check_checkouts() -> None:
        for repo, root in roots.items():
            if observe(root) != observations[repo]:
                raise SourcePathConflict(
                    f"checkout changed while indexing {repo!r}; retry the batch"
                )

    def validate(candidate: GraphSnapshot) -> None:
        current = store.load()
        for repo, root in roots.items():
            check_source_identity(current, repo, root)
            validate_golden(candidate, root)
        link_losses.extend(lost_cross_repo_links(current, candidate, set(roots)))
        check_checkouts()

    def check_before_activation(done: int, total: int) -> None:
        if done == total - 1:
            check_checkouts()

    with ExitStack() as locks:
        for repo in sorted(roots):
            locks.enter_context(JOBS.index_lock(str(roots[repo]), repo))
        for repo, root in roots.items():
            check_source_identity(store.load(), repo, root)
            observations[repo] = observe(root)
            snapshot, service_map = index_repo(root, commit=observations[repo][0])
            if service_map.repo != repo:
                raise SourcePathConflict(f"checkout identity changed while indexing {root}")
            for node_id, node in snapshot.nodes.items():
                if node_id in replacement.nodes and node.kind != NodeKind.EXTERNAL_API:
                    raise ValueError(f"duplicate graph node ID in batch: {node_id}")
                replacement.nodes[node_id] = node
            replacement.edges.extend(snapshot.edges)
            replacement.unresolved.extend(snapshot.unresolved)
            replacement.diagnostics.extend(snapshot.diagnostics)
            replacement.link_inputs.update(snapshot.link_inputs)
            replacement.link_reports.update(snapshot.link_reports)
        if isinstance(store, Neo4jStore):
            store.publish_progress = check_before_activation
        try:
            committed = store.publish_repositories(replacement, validate=validate)
        finally:
            if isinstance(store, Neo4jStore):
                store.publish_progress = None
        return committed, sorted(roots), link_losses
