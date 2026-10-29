import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from axon.api.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_db():
    with patch("axon.api.routes.breaking.async_session") as m_async:
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
            if "MATCH (a:BreakingChangeAlert)" in query:
                return MockResult(data=[{
                    "a": {
                        "id": "alert1",
                        "alert_type": "API_SCHEMA",
                        "severity": "CRITICAL",
                        "commit_sha": "abc",
                        "node_id": "ep1",
                        "affected_teams": ["t1"],
                        "affected_targets": ["//a:b"],
                        "is_acknowledged": False,
                        "created_at": datetime.now()
                    }
                }])
            elif "SET a.is_acknowledged = true" in query:
                if kwargs.get("alert_id") == "bad":
                    return MockResult()
                return MockResult(single={
                    "a": {
                        "id": kwargs.get("alert_id"),
                        "is_acknowledged": True,
                        "acknowledged_by": kwargs.get("by"),
                        "acknowledged_at": datetime.now()
                    }
                })
            return MockResult()
            
        session_mock.run.side_effect = mock_run
        yield m_async


def test_get_alerts() -> None:
    response = client.get("/api/v1/breaking/alerts?commit=abc")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == "alert1"


def test_acknowledge_alert() -> None:
    response = client.post("/api/v1/breaking/alerts/alert1/acknowledge", json={"acknowledged_by": "alice"})
    assert response.status_code == 200
    data = response.json()
    assert data["is_acknowledged"] is True
    assert data["acknowledged_by"] == "alice"


def test_acknowledge_alert_404() -> None:
    response = client.post("/api/v1/breaking/alerts/bad/acknowledge", json={"acknowledged_by": "alice"})
    assert response.status_code == 404
