"""
MavenWorker: parses pom.xml from genrule targets with has_maven_src=true,
writes MavenArtifact nodes and MAVEN_DEP edges.
"""

from __future__ import annotations

import argparse
import logging
import sys

from axon.db import writers
from axon.db.connection import sync_session
from axon.models.enums import EdgeConfidence
from axon.workers.base import BaseWorker, WorkerResult
from axon.workers.maven.parser import parse_pom_file

logger = logging.getLogger(__name__)


def _find_pom_src(srcs: list[str]) -> str | None:
    """Find the pom.xml path from genrule srcs."""
    for src in srcs:
        if "pom.xml" in src.lower() or "pom" in src.lower():
            if src.startswith("//"):
                src = src.lstrip("/")
                src = src.replace(":", "/")
            return src
    return None


class MavenWorker(BaseWorker):
    """Indexes Maven dependencies from genrule targets."""

    def __init__(self, workspace_root: str, commit_sha: str | None = None) -> None:
        super().__init__(workspace_root)
        self.commit_sha = commit_sha

    def run(self) -> WorkerResult:
        result = WorkerResult(worker_name="MavenWorker")
        logger.info("MavenWorker starting (workspace=%s)", self.workspace_root)

        try:
            targets = self._get_maven_targets()
            logger.info("Found %d maven genrule targets", len(targets))

            artifact_dicts = []
            edge_dicts = []
            seen_artifacts: set[str] = set()

            for target in targets:
                label = target["label"]
                srcs = target.get("genrule_srcs") or []
                target_id = target["id"]

                pom_path = _find_pom_src(srcs)
                if not pom_path:
                    result.warnings.append(f"No pom.xml found for {label}")
                    continue

                deps = parse_pom_file(pom_path, self.workspace_root)
                for dep in deps:
                    art_id = f"maven::{dep.group_id}:{dep.artifact_id}:{dep.version}"

                    if art_id not in seen_artifacts:
                        seen_artifacts.add(art_id)
                        artifact_dicts.append(
                            {
                                "id": art_id,
                                "group_id": dep.group_id,
                                "artifact_id": dep.artifact_id,
                                "version": dep.version,
                                "scope": dep.scope.upper(),
                            }
                        )

                    edge_dicts.append(
                        {
                            "from_id": target_id,
                            "to_id": art_id,
                            "scope": dep.scope.upper(),
                            "declared_in": pom_path,
                            "confidence": EdgeConfidence.EXTRACTED,
                            "valid_from_commit": self.commit_sha,
                        }
                    )

            result.nodes_written += writers.write_maven_artifacts(artifact_dicts)
            result.edges_written += writers.write_maven_dep_edges(edge_dicts)

        except Exception as e:
            logger.exception("MavenWorker failed: %s", e)
            result.errors.append(str(e))
            return result.finish(success=False)

        logger.info(
            "MavenWorker complete: %d nodes, %d edges", result.nodes_written, result.edges_written
        )
        return result.finish(success=True)

    def _get_maven_targets(self) -> list[dict]:
        with sync_session() as session:
            return session.run("""
                MATCH (t:BazelTarget)
                WHERE t.has_maven_src = true AND t.is_genrule = true
                RETURN t.id AS id, t.label AS label, t.genrule_srcs AS genrule_srcs
            """).data()


def cli_main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    ap = argparse.ArgumentParser(description="axon: index Maven dependencies")
    ap.add_argument("--workspace", required=True, help="Path to the monorepo root")
    ap.add_argument("--commit", default=None, help="Git commit SHA")
    args = ap.parse_args()

    worker = MavenWorker(workspace_root=args.workspace, commit_sha=args.commit)
    result = worker.run()

    status = "✓" if result.success else "✗"
    print(f"\n{status} MavenWorker {'succeeded' if result.success else 'FAILED'}")
    print(
        f"  Nodes: {result.nodes_written}  Edges: {result.edges_written}  Duration: {result.duration_ms} ms"
    )
    if result.errors:
        for e in result.errors:
            print(f"  ERROR: {e}")
        sys.exit(1)
