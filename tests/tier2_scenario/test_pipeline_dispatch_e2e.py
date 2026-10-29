"""Dispatcher routing and pipeline idempotency tests."""

from __future__ import annotations

import pytest

from axon.pipeline.dispatcher import WorkerKind, dispatch

pytestmark = pytest.mark.tier2


def test_empty_files_is_full_reindex() -> None:
    plan = dispatch([])
    assert plan.is_full_reindex is True
    assert WorkerKind.BAZEL in plan.workers
    assert WorkerKind.TREESITTER_PYTHON in plan.workers
    assert WorkerKind.SWAGGER in plan.workers


def test_build_file_routes_to_bazel() -> None:
    plan = dispatch(["services/payment/BUILD"])
    assert WorkerKind.BAZEL in plan.workers
    assert WorkerKind.TREESITTER_PYTHON not in plan.workers
    assert "//services/payment" in plan.bazel_packages


def test_swagger_routes_to_swagger_and_bazel() -> None:
    plan = dispatch(["services/payment/swagger.json"])
    assert WorkerKind.SWAGGER in plan.workers
    assert WorkerKind.BAZEL in plan.workers


def test_worker_order_is_bazel_first() -> None:
    plan = dispatch(["services/payment/BUILD", "services/payment/src/payment.py"])
    assert plan.workers[0] == WorkerKind.BAZEL


@pytest.mark.asyncio
async def test_second_push_with_same_sha_is_noop(monorepo_root) -> None:
    from axon.pipeline.coordinator import PipelineCoordinator
    from axon.pipeline.event_schema import PushEvent
    from tests.tier2_scenario.conftest import COMMIT_SHA

    result = await PipelineCoordinator().process(
        PushEvent(sha=COMMIT_SHA, branch="main", workspace_root=monorepo_root)
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "already_indexed"
