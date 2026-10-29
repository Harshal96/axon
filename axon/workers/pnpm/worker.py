"""
PnpmWorker: processes pnpm-lock.yaml and workspace package.json files.

Writes to Neo4j:
  - PnpmPackage nodes: one per unique package (internal and external)
  - PNPM_DEP edges: BazelTarget → PnpmPackage

The internal workspace packages are the most important output: they are
the bridge that allows TSWorker's import resolver to link TypeScript
import statements to Bazel targets.
"""

from __future__ import annotations

import logging
import os

from axon.db import writers
from axon.db.connection import sync_session
from axon.workers.base import BaseWorker, WorkerResult
from axon.workers.pnpm.lockfile_parser import parse_lockfile
from axon.workers.pnpm.workspace_resolver import build_workspace_map

logger = logging.getLogger(__name__)

_FETCH_PNPM_GENRULES = """
MATCH (t:BazelTarget)
WHERE t.has_pnpm_src = true AND t.is_genrule = true
RETURN t.label AS label, t.package AS package,
       t.genrule_srcs AS srcs
"""


class PnpmWorker(BaseWorker):
    def __init__(
        self,
        workspace_root: str,
        commit_sha: str | None = None,
        lockfile_paths: list[str] | None = None,
    ) -> None:
        super().__init__(workspace_root)
        self.commit_sha = commit_sha
        self.lockfile_paths = lockfile_paths

    def run(self) -> WorkerResult:
        result = WorkerResult(worker_name="PnpmWorker")
        logger.info("PnpmWorker starting (workspace=%s)", self.workspace_root)

        try:
            # ── Step 1: find lockfile(s) ──────────────────────────────────────
            lockfile_paths = self.lockfile_paths or self._discover_lockfiles()
            logger.info("Step 1: %d lockfile(s) to process", len(lockfile_paths))

            all_pkg_dicts: list[dict] = []
            all_pnpm_dep_edges: list[dict] = []

            for lockfile_path in lockfile_paths:
                full_path = (
                    os.path.join(self.workspace_root, lockfile_path)
                    if not os.path.isabs(lockfile_path)
                    else lockfile_path
                )

                # ── Step 2: parse lockfile ─────────────────────────────────────
                parsed = parse_lockfile(full_path)
                for err in parsed.parse_errors:
                    result.warnings.append(err)

                # ── Step 3: resolve workspace packages → Bazel labels ──────────
                workspace_map = build_workspace_map(parsed.workspace_packages, self.workspace_root)
                logger.info("Step 3: %d workspace packages resolved", len(workspace_map))

                # ── Step 4: build PnpmPackage dicts for internal packages ───────
                for pkg_name, pkg_info in workspace_map.items():
                    pkg_id = f"pnpm::{pkg_name}@workspace"
                    all_pkg_dicts.append(
                        {
                            "id": pkg_id,
                            "name": pkg_name,
                            "version": "workspace",
                            "workspace_path": pkg_info["path"],
                            "is_internal": True,
                            "bazel_label": pkg_info["bazel_label"],
                        }
                    )

                    # PNPM_DEP edge: BazelTarget (genrule consumer) → PnpmPackage
                    # We'll also link from workspace package's own Bazel target
                    if pkg_info["bazel_label"]:
                        all_pnpm_dep_edges.append(
                            {
                                "from_id": f"target::{pkg_info['bazel_label']}",
                                "to_id": pkg_id,
                                "declared_in": lockfile_path,
                            }
                        )

                # ── Step 5: build PnpmPackage dicts for external packages ───────
                seen_externals: set[str] = set()
                for dep in parsed.all_external_packages:
                    pkg_id = f"pnpm::{dep.name}@{dep.version}"
                    if pkg_id in seen_externals:
                        continue
                    seen_externals.add(pkg_id)
                    all_pkg_dicts.append(
                        {
                            "id": pkg_id,
                            "name": dep.name,
                            "version": dep.version,
                            "workspace_path": None,
                            "is_internal": False,
                            "bazel_label": None,
                        }
                    )

                # ── Step 6: link genrule consumers to their pnpm packages ────────
                with sync_session() as session:
                    genrule_records = session.run(_FETCH_PNPM_GENRULES).data()

                for rec in genrule_records:
                    target_label = rec["label"]
                    pkg_path = rec["package"].lstrip("/")

                    # Find the workspace package(s) that this genrule is
                    # located in or depends on
                    for pkg_name, pkg_info in workspace_map.items():
                        if pkg_info["path"] == pkg_path or pkg_path.startswith(pkg_info["path"]):
                            pkg_id = f"pnpm::{pkg_name}@workspace"
                            all_pnpm_dep_edges.append(
                                {
                                    "from_id": f"target::{target_label}",
                                    "to_id": pkg_id,
                                    "declared_in": lockfile_path,
                                }
                            )

            # ── Step 7: write ─────────────────────────────────────────────────
            # Deduplicate
            seen_ids: set[str] = set()
            unique_pkgs = []
            for d in all_pkg_dicts:
                if d["id"] not in seen_ids:
                    seen_ids.add(d["id"])
                    unique_pkgs.append(d)

            logger.info(
                "Writing %d PnpmPackage nodes and %d PNPM_DEP edges ...",
                len(unique_pkgs),
                len(all_pnpm_dep_edges),
            )
            result.nodes_written += writers.write_pnpm_packages(unique_pkgs)
            result.edges_written += writers.write_pnpm_dep_edges(all_pnpm_dep_edges)

        except Exception as e:
            logger.exception("PnpmWorker failed: %s", e)
            result.errors.append(str(e))
            return result.finish(success=False)

        logger.info(
            "PnpmWorker complete: %d nodes, %d edges",
            result.nodes_written,
            result.edges_written,
        )
        return result.finish(success=True)

    def _discover_lockfiles(self) -> list[str]:
        """Find all pnpm-lock.yaml files in the workspace."""
        lockfiles = []
        for dirpath, dirnames, filenames in os.walk(self.workspace_root):
            dirnames[:] = [
                d for d in dirnames if d not in ("node_modules", ".git", ".cache", "dist")
            ]
            for fn in filenames:
                if fn == "pnpm-lock.yaml":
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, self.workspace_root)
                    lockfiles.append(rel)
        return lockfiles
