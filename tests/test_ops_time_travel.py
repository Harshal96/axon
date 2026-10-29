import pytest
from unittest.mock import AsyncMock, patch

from axon.ops.time_travel import time_travel_blast_radius
from axon.history.models import TimeTravelBlastRadiusResult, TimeTravelMeta


@pytest.fixture(autouse=True)
def mock_db():
    with patch("axon.ops.time_travel.blast_radius_at_commit") as m_blast, \
         patch("axon.ops.time_travel.get_commit_timestamp") as m_ts, \
         patch("axon.ops.time_travel.get_graph_meta") as m_meta, \
         patch("axon.db.connection.async_session") as m_async:
        
        from tests.fixtures.history_seed import TS_A, TS_B, SHA_A, SHA_B
        
        async def mock_ts(sha):
            if sha == SHA_A: return TS_A
            if sha == SHA_B: return TS_B
            return None
        m_ts.side_effect = mock_ts
        
        m_blast.return_value = ([{"label": "a", "kind": "PY", "team": "t", "depth": 1, "dep_type": "DIRECT"}], 1)
        
        from unittest.mock import MagicMock
        m_meta_obj = MagicMock()
        m_meta_obj.model_dump.return_value = {"graph_version": SHA_A}
        m_meta.return_value = m_meta_obj
        
        session_mock = AsyncMock()
        m_async.return_value.__aenter__.return_value = session_mock
        res = AsyncMock()
        res.single.return_value = {"behind_count": 1}
        session_mock.run.return_value = res
        
        yield


@pytest.mark.asyncio
async def test_time_travel_returns_result() -> None:
    from tests.fixtures.history_seed import SHA_A
    result = await time_travel_blast_radius(
        label="//services/payment:payment_lib",
        at_commit=SHA_A,
        depth=3,
    )
    assert result.time_travel.at_commit == SHA_A
    assert isinstance(result.affected_targets, list)


@pytest.mark.asyncio
async def test_time_travel_meta_has_commits_behind() -> None:
    from tests.fixtures.history_seed import SHA_A
    result = await time_travel_blast_radius(
        label="//services/payment:payment_lib",
        at_commit=SHA_A,
    )
    assert result.time_travel.commits_behind_head >= 1


@pytest.mark.asyncio
async def test_time_travel_graph_version_is_at_commit() -> None:
    from tests.fixtures.history_seed import SHA_A
    result = await time_travel_blast_radius(
        label="//services/payment:payment_lib",
        at_commit=SHA_A,
    )
    # the mock meta is returned, we override the returned object's meta
    # in time_travel_blast_radius it sets meta.graph_version = at_commit
    # we just check if it's there
    assert result.meta["graph_version"] == SHA_A


@pytest.mark.asyncio
async def test_time_travel_raises_on_unknown_commit() -> None:
    with pytest.raises(ValueError, match="not found"):
        await time_travel_blast_radius(
            label="//services/payment:payment_lib",
            at_commit="dead0000dead0000dead0000dead0000dead0000",
        )


@pytest.mark.asyncio
async def test_time_travel_includes_warning() -> None:
    from tests.fixtures.history_seed import SHA_A
    result = await time_travel_blast_radius(
        label="//services/payment:payment_lib",
        at_commit=SHA_A,
    )
    assert any(
        w["code"] == "TIME_TRAVEL_QUERY" for w in result.warnings
    )