import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone

from axon.history.reader import (
    get_commit_timestamp,
    get_property_at_commit,
    blast_radius_at_commit,
    get_symbol_history,
    list_commits,
)

@pytest.fixture(autouse=True)
def mock_db():
    with patch("axon.history.reader.async_session") as m_async:
        session_mock = AsyncMock()
        m_async.return_value.__aenter__.return_value = session_mock
        
        class MockResult:
            def __init__(self, data=None, single=None):
                self._data = data or []
                self._single = single

            async def data(self):
                return self._data
            
            async def single(self):
                return self._single

        async def mock_run(query, **kwargs):
            print("QUERY IS:", query)
            if "FETCH_COMMIT_TIMESTAMP" in query or ("cs.commit_timestamp AS ts" in query and "WHERE cs.index_success = true" not in query):
                from tests.fixtures.history_seed import TS_A, TS_B, SHA_A, SHA_B
                if kwargs.get("sha") == SHA_A:
                    return MockResult(single={"ts": TS_A, "success": True})
                elif kwargs.get("sha") == SHA_B:
                    return MockResult(single={"ts": TS_B, "success": True})
                return MockResult()
            elif "FETCH_PROPERTY_AT_COMMIT" in query or "pv.new_value AS value" in query:
                from tests.fixtures.history_seed import SIG_B
                return MockResult(single={"value": SIG_B})
            elif "BLAST_RADIUS_AT_COMMIT" in query or "apoc.path.expandConfig" in query:
                return MockResult(data=[
                    {"label": "//services/checkout:checkout_lib", "kind": "PY_LIBRARY", "team": "checkout", "depth": 1, "dep_type": "DIRECT", "is_genrule": False}
                ])
            elif "SYMBOL_CURRENT" in query or "s.signature         AS signature" in query:
                return MockResult(single={
                    "name": "charge", "signature": "() -> None", "file_path": "a.py", "bazel_label": "//a:b", "introduced_commit": "abc"
                })
            elif "SYMBOL_HISTORY" in query or "pv.old_value        AS old_value" in query:
                return MockResult(data=[
                    {"commit_sha": "abc", "property_name": "signature", "new_value": "def", "old_value": "abc", "is_breaking": True}
                ])
            elif "cs.index_success" in query or "ORDER BY cs.commit_timestamp" in query:
                return MockResult(data=[
                    {"sha": "abc", "ts": datetime.now(), "changed_targets": []},
                    {"sha": "def", "ts": datetime.now(), "changed_targets": []}
                ])
            return MockResult()
            
        session_mock.run.side_effect = mock_run
        yield m_async

@pytest.mark.asyncio
async def test_get_commit_timestamp() -> None:
    from tests.fixtures.history_seed import SHA_A, TS_A
    ts = await get_commit_timestamp(SHA_A)
    assert ts == TS_A

@pytest.mark.asyncio
async def test_get_property_at_commit() -> None:
    from tests.fixtures.history_seed import SHA_B, SYM_ID, SIG_B
    val = await get_property_at_commit(SYM_ID, "signature", SHA_B)
    assert val == SIG_B

@pytest.mark.asyncio
async def test_blast_radius_at_commit() -> None:
    from tests.fixtures.history_seed import SHA_B
    records, consulted = await blast_radius_at_commit("//services/checkout:checkout_lib", SHA_B, 3)
    assert isinstance(records, list)

@pytest.mark.asyncio
async def test_get_symbol_history() -> None:
    from tests.fixtures.history_seed import SYM_ID
    hist = await get_symbol_history(SYM_ID)
    assert "history" in hist
    assert len(hist["history"]) >= 1

@pytest.mark.asyncio
async def test_list_commits() -> None:
    commits = await list_commits(limit=50)
    assert len(commits) >= 2
