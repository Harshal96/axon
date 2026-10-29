"""Property-based integration contracts for critical pure helpers."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from axon.api.cache import _cache_key
from axon.pipeline.dispatcher import WorkerKind, dispatch

pytestmark = pytest.mark.tier1


class TestDispatcherProperties:
    @given(st.lists(st.text(min_size=1, max_size=100), max_size=50))
    @settings(suppress_health_check=[HealthCheck.too_slow])
    def test_dispatcher_never_raises(self, file_paths: list[str]) -> None:
        plan = dispatch(file_paths)
        assert isinstance(plan.workers, list)

    @given(
        st.lists(
            st.sampled_from(
                ["BUILD", "BUILD.bazel", "foo.py", "bar.java", "swagger.json", "pom.xml"]
            ),
            min_size=1,
            max_size=10,
        )
    )
    def test_build_files_always_include_bazel_worker(self, filenames: list[str]) -> None:
        paths = [f"services/payment/{name}" for name in filenames]
        plan = dispatch(paths)
        if any(name in {"BUILD", "BUILD.bazel"} for name in filenames):
            assert WorkerKind.BAZEL in plan.workers

    @given(st.lists(st.text(min_size=1, max_size=100), max_size=50))
    def test_worker_order_always_bazel_first(self, file_paths: list[str]) -> None:
        plan = dispatch(file_paths)
        if WorkerKind.BAZEL in plan.workers and len(plan.workers) > 1:
            assert plan.workers[0] == WorkerKind.BAZEL


class TestCacheKeyProperties:
    @given(
        st.text(min_size=1, max_size=50),
        st.dictionaries(
            st.text(min_size=1, max_size=20),
            st.one_of(st.text(), st.integers(), st.booleans()),
            max_size=10,
        ),
    )
    def test_cache_key_is_deterministic(self, op_name: str, params: dict) -> None:
        assert _cache_key(op_name, params) == _cache_key(op_name, params)

    @given(
        st.text(min_size=1, max_size=50),
        st.dictionaries(st.text(), st.text(), max_size=5),
        st.dictionaries(st.text(), st.text(), max_size=5),
    )
    def test_different_params_produce_different_keys(
        self, op_name: str, params1: dict, params2: dict
    ) -> None:
        if params1 == params2:
            return
        assert _cache_key(op_name, params1) != _cache_key(op_name, params2)
