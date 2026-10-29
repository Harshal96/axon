from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime

from axon.pipeline.commit_writer import is_commit_indexed, write_commit_snapshot
from axon.pipeline.dispatcher import WorkerKind, dispatch
from axon.pipeline.event_schema import PushEvent
from axon.workers.base import WorkerResult

logger = logging.getLogger(__name__)


def invalidate_redis_cache() -> None:
    logger.info("Invalidating Redis cache (mock)")


def broadcast_graph_updated(sha: str) -> None:
    logger.info("Broadcasting GRAPH_UPDATED for %s (mock)", sha)


class PipelineCoordinator:
    async def process(self, event: PushEvent) -> dict:
        logger.info(
            "Starting pipeline for %s with %d changed files",
            event.sha,
            len(event.changed_files),
        )

        if await is_commit_indexed(event.sha):
            logger.info("Commit %s already indexed. Skipping.", event.sha)
            return {"status": "skipped", "reason": "already_indexed", "sha": event.sha}

        plan = dispatch(event.changed_files)
        logger.info("Workers to run: %s", [w.value for w in plan.workers])

        start = time.monotonic()
        success = True
        worker_results: list[WorkerResult] = []

        for worker_kind in plan.workers:
            res = self._run_worker(worker_kind, event, plan.bazel_packages or None)
            worker_results.append(res)
            if not res.success:
                success = False
                break

        duration_ms = int((time.monotonic() - start) * 1000)
        await write_commit_snapshot(event.sha, success, duration_ms)

        if success:
            from axon.history.writer import record_changes

            history_stats = await record_changes(
                changed_target_labels=_collect_changed_targets(event.changed_files),
                commit_sha=event.sha,
                commit_timestamp=datetime.now(tz=UTC),
                author=event.author or None,
            )
            logger.info(
                "History: %d property versions written",
                history_stats.get("property_versions_written", 0),
            )
            invalidate_redis_cache()
            broadcast_graph_updated(event.sha)

        return {
            "status": "indexed" if success else "indexed_with_errors",
            "sha": event.sha,
            "duration_ms": duration_ms,
            "plan": asdict(plan),
            "workers": [
                {
                    "worker": r.worker_name,
                    "success": r.success,
                    "nodes_written": r.nodes_written,
                    "edges_written": r.edges_written,
                    "errors": r.errors,
                }
                for r in worker_results
            ],
        }

    def _run_worker(
        self,
        worker_kind: WorkerKind,
        event: PushEvent,
        bazel_packages: list[str] | None,
    ) -> WorkerResult:
        if worker_kind == WorkerKind.BAZEL:
            from axon.workers.bazel.worker import BazelWorker

            return BazelWorker(
                workspace_root=event.workspace_root,
                commit_sha=event.sha,
                packages=bazel_packages,
            ).run()

        if worker_kind == WorkerKind.TREESITTER_PYTHON:
            from axon.workers.treesitter.worker import TreeSitterWorker

            return TreeSitterWorker(
                workspace_root=event.workspace_root,
                commit_sha=event.sha,
                packages=None,
            ).run()

        if worker_kind == WorkerKind.TREESITTER_JAVA:
            from axon.workers.java.worker import JavaWorker

            return JavaWorker(
                workspace_root=event.workspace_root,
                commit_sha=event.sha,
                target_labels=None,
            ).run()

        if worker_kind == WorkerKind.TREESITTER_JS:
            from axon.workers.ts.worker import TSWorker

            return TSWorker(
                workspace_root=event.workspace_root,
                commit_sha=event.sha,
                file_paths=None,
            ).run()

        if worker_kind == WorkerKind.SWAGGER:
            from axon.workers.swagger.worker import SwaggerWorker

            return SwaggerWorker(
                workspace_root=event.workspace_root,
                commit_sha=event.sha,
            ).run()

        if worker_kind == WorkerKind.MAVEN:
            from axon.workers.java.maven.worker import MavenWorker

            return MavenWorker(
                workspace_root=event.workspace_root,
                commit_sha=event.sha,
                pom_paths=None,
                enable_vuln_scan=True,
            ).run()

        if worker_kind == WorkerKind.PNPM:
            from axon.workers.pnpm.worker import PnpmWorker

            return PnpmWorker(
                workspace_root=event.workspace_root,
                commit_sha=event.sha,
                lockfile_paths=None,
            ).run()

        return WorkerResult(worker_name=worker_kind.value).finish(success=True)


def _collect_changed_targets(changed_files: list[str]) -> list[str]:
    return [path for path in changed_files if path.startswith("//")]


async def run_pipeline_async(sha: str, changed_files: list[str]) -> None:
    event = PushEvent(sha=sha, changed_files=changed_files)
    await PipelineCoordinator().process(event)
