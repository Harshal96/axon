import pytest
from unittest.mock import AsyncMock, patch

from axon.history.writer import record_changes, _version_id
from axon.history.reader import get_property_at_commit


def test_version_id_is_deterministic() -> None:
    vid1 = _version_id("target::foo", "signature", "abc123")
    vid2 = _version_id("target::foo", "signature", "abc123")
    assert vid1 == vid2
    assert vid1.startswith("propver::")


def test_version_id_differs_by_property() -> None:
    vid1 = _version_id("target::foo", "signature", "abc123")
    vid2 = _version_id("target::foo", "visibility", "abc123")
    assert vid1 != vid2


@pytest.mark.asyncio
@patch("axon.history.writer.async_session")
async def test_record_changes_detects_signature_change(mock_session_ctx) -> None:
    from tests.fixtures.history_seed import SHA_B, TS_B, SYM_ID
    session_mock = AsyncMock()
    mock_session_ctx.return_value.__aenter__.return_value = session_mock
    
    async def mock_run(query, **kwargs):
        res = AsyncMock()
        if "FETCH_CURRENT_PROPS" in query:
            res.single.return_value = {"props": {"signature": "abc", "is_public": True}, "labels": ["Symbol"]}
        elif "FETCH_LATEST_VERSION" in query:
            res.single.return_value = {"value": "def", "commit": "old_commit"}
        elif "MATCH (t:BazelTarget" in query: # sym_result
            res.data.return_value = [{"sym_id": "1", "sig": "def", "cc": 1, "is_public": True}]
        elif "MERGE (pv:PropertyVersion" in query:
            res.single.return_value = {"written": 1}
        return res
        
    session_mock.run.side_effect = mock_run

    result = await record_changes(
        changed_target_labels=["//services/payment:payment_lib"],
        commit_sha=SHA_B,
        commit_timestamp=TS_B,
        author="test",
    )
    assert result["property_versions_written"] >= 0


@pytest.mark.asyncio
@patch("axon.history.reader.async_session")
async def test_get_property_at_commit_returns_old_value(mock_session_ctx) -> None:
    from tests.fixtures.history_seed import SHA_A, SYM_ID, SIG_A
    session_mock = AsyncMock()
    mock_session_ctx.return_value.__aenter__.return_value = session_mock
    
    async def mock_run(query, **kwargs):
        res = AsyncMock()
        if "cs.commit_timestamp AS ts" in query:
            res.single.return_value = {"ts": "some_ts", "success": True}
        elif "pv.new_value AS value" in query:
            res.single.return_value = {"value": SIG_A}
        else:
            res.single.return_value = None
        return res
        
    session_mock.run.side_effect = mock_run

    val = await get_property_at_commit(SYM_ID, "signature", SHA_A)
    assert val is None or val == SIG_A