"""
Tests for traversal operations.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from axon.graph.models import GraphMeta
from axon.graph.traversal import affected_tests, callees, callers, dependency_path, subgraph


def _mock_session_router():
    mock_session = AsyncMock()

    async def mock_run(*args, **kwargs):
        query = args[0] if args else kwargs.get("query", "")
        mock_result = AsyncMock()
        records = []
        if "MATCH path = shortestPath" in query:
            records = [{"path_labels": ["//a:a", "//b:b", "//c:c"], "hops": 2}]
        elif "MATCH (target:Symbol {id: $symbol_id})" in query:
            records = [
                {
                    "caller": {
                        "id": "f.py::foo",
                        "name": "foo",
                        "qualified_name": "mod.foo",
                        "kind": "FUNCTION",
                    },
                    "call_depth": 1,
                    "file_path": "mod.py",
                }
            ]
        elif "MATCH (source:Symbol {id: $symbol_id})" in query:
            records = [
                {
                    "callee": {
                        "id": "f.py::baz",
                        "name": "baz",
                        "qualified_name": "mod.baz",
                        "kind": "FUNCTION",
                    },
                    "call_depth": 1,
                    "file_path": "mod.py",
                }
            ]
        elif (
            "UNWIND $labels AS lbl\nMATCH (t:BazelTarget {label: lbl})" in query
            and "sf.path" in query
        ):
            records = [
                {
                    "label": "//a:a",
                    "kind": "PY_LIBRARY",
                    "team": "team-x",
                    "src_files": [],
                    "symbols": [],
                }
            ]
        elif "UNWIND $file_paths AS fp" in query:
            records = [
                {
                    "label": "//services/payment:payment_test",
                    "via_file": "services/payment/payment.py",
                }
            ]
        elif "UNWIND $root_labels AS root" in query:
            records = [
                {
                    "label": "//services/payment:payment_test",
                    "kind": "PY_TEST",
                    "team": "payments-team",
                    "via_target": "//services/payment:payment_test",
                }
            ]

        mock_result.data = AsyncMock(return_value=records)
        mock_result.single = AsyncMock(return_value=records[0] if records else None)
        return mock_result

    mock_session.run = mock_run
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _mock_session_empty():
    mock_session = AsyncMock()
    mock_result = AsyncMock()
    mock_result.data = AsyncMock(return_value=[])
    mock_result.single = AsyncMock(return_value=None)
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_dependency_path_found() -> None:
    with patch("axon.graph.traversal.async_session", return_value=_mock_session_router()):
        resp = await dependency_path("//a:a", "//c:c")
    assert resp.found is True


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_dependency_path_not_found() -> None:
    with patch("axon.graph.traversal.async_session", return_value=_mock_session_empty()):
        resp = await dependency_path("//a:a", "//z:z")
    assert resp.found is False


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_callers_returns_list() -> None:
    with patch("axon.graph.traversal.async_session", return_value=_mock_session_router()):
        resp = await callers("f.py::bar", depth=1)
    assert resp.total == 1


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_callers_empty() -> None:
    with patch("axon.graph.traversal.async_session", return_value=_mock_session_empty()):
        resp = await callers("x::y")
    assert resp.total == 0


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_callees_returns_list() -> None:
    with patch("axon.graph.traversal.async_session", return_value=_mock_session_router()):
        resp = await callees("f.py::main", depth=2)
    assert resp.total == 1


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_subgraph_returns_targets() -> None:
    with patch("axon.graph.traversal.async_session", return_value=_mock_session_router()):
        resp = await subgraph(["//a:a"], depth=2)
    assert resp.scope["included_targets"] == 1


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_affected_tests_returns_test_targets() -> None:
    with patch("axon.graph.traversal.async_session", return_value=_mock_session_router()):
        resp = await affected_tests(["services/payment/payment.py"])
    assert resp.total == 1


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_affected_tests_no_tests() -> None:
    with patch("axon.graph.traversal.async_session", return_value=_mock_session_empty()):
        resp = await affected_tests(["some/file.py"])
    assert resp.total == 0
