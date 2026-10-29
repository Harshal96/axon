import pytest
from unittest.mock import AsyncMock, patch
from axon.history.models import GraphDiffResult

from axon.ops.graph_diff import graph_diff


@pytest.fixture(autouse=True)
def mock_db():
    with patch("axon.ops.graph_diff.compute_diff") as m_diff, \
         patch("axon.ops.graph_diff.get_graph_meta") as m_meta:
        m_meta.return_value = AsyncMock(query_duration_ms=0)
        
        from tests.fixtures.history_seed import TS_A, TS_B, SHA_A, SHA_B
        res = GraphDiffResult(
            sha1=SHA_A, sha2=SHA_B, sha1_timestamp=TS_A, sha2_timestamp=TS_B,
            added_targets=[], removed_targets=[], changed_symbols=[
                {"symbol_id": "1", "name": "a", "file_path": "a", "bazel_label": "lbl", "property_changed": "a", "new_value": "b", "old_value": "a", "is_breaking": True}
            ],
            added_edges=[], removed_edges=[], api_schema_changes=[],
            summary={"changed_symbols": 1}
        )
        m_diff.return_value = res
        yield


@pytest.mark.asyncio
async def test_ops_graph_diff() -> None:
    from tests.fixtures.history_seed import SHA_A, SHA_B
    result = await graph_diff(SHA_A, SHA_B)
    assert result.sha1 == SHA_A
    assert result.sha2 == SHA_B
    assert "changed_symbols" in result.summary
