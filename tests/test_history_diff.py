import pytest
from unittest.mock import AsyncMock, patch

from axon.history.diff import compute_diff


@pytest.fixture(autouse=True)
def mock_db():
    with patch("axon.history.diff.async_session") as m_async, \
         patch("axon.history.diff.get_commit_timestamp") as m_ts:
        
        session_mock = AsyncMock()
        m_async.return_value.__aenter__.return_value = session_mock
        
        from tests.fixtures.history_seed import TS_A, TS_B, SHA_A, SHA_B
        
        async def mock_ts(sha):
            if sha == SHA_A: return TS_A
            if sha == SHA_B: return TS_B
            return None
        m_ts.side_effect = mock_ts
        
        async def mock_run(query, **kwargs):
            res = AsyncMock()
            if "TARGETS_ADDED" in query or "OPTIONAL MATCH (cs_from:CommitSnapshot)<-[:INTRODUCED_BY]-(t)" in query:
                res.data.return_value = [{"label": "foo", "kind": "bar", "team": "baz", "added_at_commit": "SHA_B", "has_swagger": False, "is_genrule": False}]
            elif "TARGETS_REMOVED" in query or "pv.new_value = 'REMOVED'" in query:
                res.data.return_value = []
            elif "SYMBOLS_CHANGED" in query or "pv.property_name IN ['signature', 'cyclomatic_complexity']" in query:
                res.data.return_value = [{"symbol_id": "1", "name": "charge", "file_path": "a.py", "property_changed": "signature", "old_value": "a", "new_value": "b", "is_breaking": True}]
            elif "EDGES_ADDED" in query or "WHERE r.valid_from_commit IS NOT NULL" in query:
                res.data.return_value = [{"from_label": "a", "to_label": "subscriptions", "edge_type": "DEPENDS_ON", "from_commit": "SHA_B"}]
            elif "EDGES_REMOVED" in query or "WHERE r.valid_until_commit IS NOT NULL" in query:
                res.data.return_value = []
            elif "API_CHANGES" in query or "pv.property_name = 'schema_hash'" in query:
                res.data.return_value = []
            else:
                res.data.return_value = []
            return res
            
        session_mock.run.side_effect = mock_run
        yield m_async


@pytest.mark.asyncio
async def test_diff_counts_changed_symbols() -> None:
    from tests.fixtures.history_seed import SHA_A, SHA_B
    result = await compute_diff(SHA_A, SHA_B)
    assert len(result.changed_symbols) >= 1
    charge_changes = [
        s for s in result.changed_symbols if "charge" in s.name
    ]
    assert len(charge_changes) >= 1
    assert charge_changes[0].is_breaking is True


@pytest.mark.asyncio
async def test_diff_counts_added_edges() -> None:
    from tests.fixtures.history_seed import SHA_A, SHA_B
    result = await compute_diff(SHA_A, SHA_B)
    added = [
        e for e in result.added_edges
        if "subscriptions" in e.to_label
    ]
    assert len(added) >= 1


@pytest.mark.asyncio
async def test_diff_summary_keys() -> None:
    from tests.fixtures.history_seed import SHA_A, SHA_B
    result = await compute_diff(SHA_A, SHA_B)
    assert "added_targets" in result.summary
    assert "changed_symbols" in result.summary
    assert "breaking_symbol_changes" in result.summary


@pytest.mark.asyncio
async def test_diff_raises_on_wrong_order() -> None:
    from tests.fixtures.history_seed import SHA_A, SHA_B
    with pytest.raises(ValueError, match="must be older"):
        await compute_diff(SHA_B, SHA_A)


@pytest.mark.asyncio
async def test_diff_raises_on_unknown_commit() -> None:
    from tests.fixtures.history_seed import SHA_A
    with pytest.raises(ValueError, match="not found"):
        await compute_diff(SHA_A, "0000000000000000000000000000000000000000")