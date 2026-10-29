import pytest
from unittest.mock import AsyncMock, patch

from axon.ops.symbol_history import symbol_history
from axon.history.models import SymbolHistoryResult, SymbolHistoryEntry


@pytest.fixture(autouse=True)
def mock_db():
    with patch("axon.ops.symbol_history.get_symbol_history") as m_hist, \
         patch("axon.ops.symbol_history.get_graph_meta") as m_meta:
        m_meta.return_value = AsyncMock(query_duration_ms=0)
        
        m_hist.return_value = {
            "name": "charge",
            "signature": "b",
            "file_path": "a.py",
            "bazel_label": "//a:b",
            "introduced_commit": "abc",
            "history": [
                {"commit_sha": "abc", "property_name": "signature", "new_value": "b", "old_value": "a", "is_breaking": True}
            ]
        }
        yield


@pytest.mark.asyncio
async def test_ops_symbol_history() -> None:
    from tests.fixtures.history_seed import SYM_ID
    result = await symbol_history(SYM_ID)
    assert result.symbol_id == SYM_ID
    assert result.total_changes >= 1
    assert result.history[0].property_name == "signature"
