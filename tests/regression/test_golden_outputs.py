"""Golden file checks for high-value graph query outputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SNAPSHOTS = Path(__file__).parent / "snapshots"
pytestmark = [pytest.mark.regression, pytest.mark.tier1]


def _load_snapshot(name: str) -> dict:
    path = SNAPSHOTS / f"{name}.json"
    if not path.exists():
        pytest.skip(
            f"Snapshot {name}.json not yet created. "
            "Run: uv run python tests/regression/update_snapshots.py"
        )
    return json.loads(path.read_text())


def _normalize(result: dict) -> dict:
    meta = result.get("meta", {})
    for key in (
        "query_duration_ms",
        "graph_indexed_at",
        "graph_version",
        "head_commit",
        "stale_by_seconds",
    ):
        meta.pop(key, None)
    result["meta"] = meta
    return result


@pytest.mark.asyncio
async def test_payment_lib_blast_radius_matches_snapshot(seed_graph) -> None:
    from axon.ops.blast_radius import blast_radius

    result = await blast_radius("//services/payment:payment_lib", depth=3)
    actual = _normalize(result.model_dump())
    expected = _load_snapshot("blast_radius_payment_lib")

    assert actual["summary"] == expected["summary"]
    actual_labels = sorted(t["label"] for t in actual["affected_targets"])
    expected_labels = sorted(t["label"] for t in expected["affected_targets"])
    assert actual_labels == expected_labels


@pytest.mark.asyncio
async def test_payment_subgraph_matches_snapshot(seed_graph) -> None:
    from axon.ops.subgraph import subgraph

    result = await subgraph(["//services/payment:payment_lib"], depth=2)
    actual = _normalize(result.model_dump())
    expected = _load_snapshot("subgraph_payment_lib")

    actual_symbols = sorted(
        symbol["name"] for target in actual["targets"] for symbol in target["key_symbols"]
    )
    expected_symbols = sorted(
        symbol["name"] for target in expected["targets"] for symbol in target["key_symbols"]
    )
    assert actual_symbols == expected_symbols
