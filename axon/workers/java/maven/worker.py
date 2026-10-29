"""
MavenWorker: processes Maven pom.xml files found in Java genrule targets.

Triggered when:
  - A pom.xml file appears in `changed_files` (Phase 7 dispatcher)
  - A BazelTarget with has_maven_src=true is newly created (full re-index)

Writes to Neo4j:
  - MavenArtifact nodes (one per unique group:artifact:version)
  - MAVEN_DEP edges from BazelTarget to MavenArtifact
  - Vulnerability data on MavenArtifact nodes (from OSS Index)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from axon.db import writers
from axon.db.connection import sync_session
from axon.workers.base import BaseWorker, WorkerResult
from axon.workers.java.maven.pom_parser import MavenCoordinate, parse_pom
from axon.workers.java.maven.runner import list_transitive_deps
from axon.workers.java.maven.vuln_scanner import scan_batch

logger = logging.getLogger(__name__)

_FETCH_MAVEN_GENRULES = """
MATCH (t:BazelTarget)
WHERE t.has_maven_src = true
  AND t.is_genrule = true
RETURN t.label AS label,
       t.package AS package,
       t.genrule_srcs AS srcs
"""


class MavenWorker(BaseWorker):
    """
    Processes all genrule targets with pom.xml srcs.
    """

    def __init__(
        self,
        workspace_root: str,
        commit_sha: str | None = None,
        pom_paths: list[str] | None = None,  # None = process all
        enable_vuln_scan: bool = True,
        oss_index_token: str | None = None,
    ) -> None:
        super().__init__(workspace_root)
        self.commit_sha = commit_sha
        self.pom_paths = pom_paths
        self.enable_vuln_scan = enable_vuln_scan
        self.oss_index_token = oss_index_token

    def run(self) -> WorkerResult:
        result = WorkerResult(worker_name="MavenWorker")
        logger.info("MavenWorker starting (workspace=%s)", self.workspace_root)

        try:
            # ── Step 1: find all Maven genrule targets in Neo4j ───────────────
            with sync_session() as session:
                records = session.run(_FETCH_MAVEN_GENRULES).data()

            logger.info("Step 1: %d Maven genrule targets found", len(records))

            all_artifact_dicts: list[dict] = []
            all_maven_dep_edges: list[dict] = []
            all_coords_for_vuln: list[MavenCoordinate] = []
            coord_to_target: dict[str, list[str]] = {}  # mvn_id → [label]

            # ── Step 2: locate and parse each pom.xml ─────────────────────────
            for rec in records:
                target_label = rec["label"]
                pom_path = self._find_pom(rec)
                if not pom_path:
                    result.warnings.append(f"No pom.xml found on disk for {target_label}")
                    continue

                if self.pom_paths and pom_path not in self.pom_paths:
                    continue

                parsed = parse_pom(pom_path)
                if parsed.parse_errors:
                    for err in parsed.parse_errors:
                        result.warnings.append(f"POM parse error in {pom_path}: {err}")

                # ── Step 3: transitive deps via mvn dependency:list ────────────
                transitive = list_transitive_deps(
                    pom_path, self.workspace_root, timeout_seconds=120
                )
                if transitive:
                    all_deps = transitive
                else:
                    # Fallback to direct deps only
                    all_deps = parsed.direct_dependencies
                    result.warnings.append(
                        f"mvn not available for {pom_path} — "
                        f"using direct deps only ({len(all_deps)} deps)"
                    )

                # ── Step 4: build artifact + edge dicts ───────────────────────
                for coord in all_deps:
                    artifact_id_neo = f"maven::{coord.group_id}:{coord.artifact_id}:{coord.version}"
                    all_artifact_dicts.append(
                        {
                            "id": artifact_id_neo,
                            "group_id": coord.group_id,
                            "artifact_id": coord.artifact_id,
                            "version": coord.version,
                            "scope": coord.scope.upper(),
                            "is_vulnerable": False,  # updated after vuln scan
                            "cve_ids": [],
                        }
                    )
                    all_maven_dep_edges.append(
                        {
                            "from_id": f"target::{target_label}",
                            "to_id": artifact_id_neo,
                            "scope": coord.scope.upper(),
                            "declared_in": pom_path,
                        }
                    )
                    if coord.mvn_id not in coord_to_target:
                        coord_to_target[coord.mvn_id] = []
                        all_coords_for_vuln.append(coord)
                    coord_to_target[coord.mvn_id].append(target_label)

            # ── Step 5: vulnerability scan ─────────────────────────────────────
            if self.enable_vuln_scan and all_coords_for_vuln:
                logger.info(
                    "Step 5: scanning %d unique artifacts against OSS Index ...",
                    len(all_coords_for_vuln),
                )
                vuln_results = asyncio.get_event_loop().run_until_complete(
                    scan_batch(all_coords_for_vuln, self.oss_index_token)
                )
                # Update is_vulnerable and cve_ids on artifact dicts
                vuln_map = {
                    f"maven::{r.coord.group_id}:{r.coord.artifact_id}:{r.coord.version}": r
                    for r in vuln_results
                }
                for art_dict in all_artifact_dicts:
                    vr = vuln_map.get(art_dict["id"])
                    if vr:
                        art_dict["is_vulnerable"] = vr.is_vulnerable
                        art_dict["cve_ids"] = vr.cve_ids

                vuln_count = sum(1 for r in vuln_results if r.is_vulnerable)
                logger.info(
                    "OSS Index: %d vulnerable artifacts found",
                    vuln_count,
                )
                if vuln_count > 0:
                    result.warnings.append(
                        f"{vuln_count} Maven artifacts have known CVEs. "
                        f"Run GET /api/v1/graph/maven?vulnerable=true for details."
                    )

            # ── Step 6: deduplicate and write ─────────────────────────────────
            seen_ids: set[str] = set()
            unique_artifacts = []
            for d in all_artifact_dicts:
                if d["id"] not in seen_ids:
                    seen_ids.add(d["id"])
                    unique_artifacts.append(d)

            logger.info(
                "Writing %d MavenArtifact nodes and %d MAVEN_DEP edges ...",
                len(unique_artifacts),
                len(all_maven_dep_edges),
            )
            result.nodes_written += writers.write_maven_artifacts(unique_artifacts)
            result.edges_written += writers.write_maven_dep_edges(all_maven_dep_edges)

        except Exception as e:
            logger.exception("MavenWorker failed: %s", e)
            result.errors.append(str(e))
            return result.finish(success=False)

        logger.info(
            "MavenWorker complete: %d nodes, %d edges",
            result.nodes_written,
            result.edges_written,
        )
        return result.finish(success=True)

    def _find_pom(self, target_rec: dict) -> str | None:
        """Locate the pom.xml on disk for a Maven genrule target."""
        package_path = target_rec["package"].lstrip("/")

        # Check srcs list first (most reliable: Bazel explicitly declared it)
        for src in target_rec.get("srcs") or []:
            src_clean = src.lstrip("/")
            # Convert label like //services/payment:pom.xml → services/payment/pom.xml
            if ":" in src_clean:
                pkg_part, file_part = src_clean.split(":", 1)
                candidate = os.path.join(self.workspace_root, pkg_part, file_part)
            else:
                candidate = os.path.join(self.workspace_root, src_clean)
            if os.path.isfile(candidate):
                return candidate

        # Fallback: standard location
        standard = os.path.join(self.workspace_root, package_path, "pom.xml")
        if os.path.isfile(standard):
            return standard

        return None


def cli_main() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    ap = argparse.ArgumentParser(description="axon: index Maven dependencies")
    ap.add_argument("--workspace", required=True, help="Path to the monorepo root")
    ap.add_argument("--commit", default=None, help="Git commit SHA being indexed")
    args = ap.parse_args()

    worker = MavenWorker(
        workspace_root=args.workspace,
        commit_sha=args.commit,
    )
    result = worker.run()

    status = "✓" if result.success else "✗"
    print(f"\n{status} MavenWorker {'succeeded' if result.success else 'FAILED'}")
    print(f"  Nodes written : {result.nodes_written}")
    print(f"  Edges written : {result.edges_written}")
    print(f"  Duration      : {result.duration_ms} ms")
    if result.errors:
        for e in result.errors:
            print(f"  ERROR: {e}")
        sys.exit(1)
