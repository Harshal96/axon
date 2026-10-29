import pytest
from unittest.mock import AsyncMock, patch

from axon.breaking.detector import classify_severity, detect_and_write


def test_classify_severity_api_schema() -> None:
    sev, kind = classify_severity("ApiEndpoint", "schema_hash", 0, 2)
    assert sev == "CRITICAL"
    assert kind == "API_SCHEMA_CHANGE"
    
    sev, kind = classify_severity("ApiEndpoint", "schema_hash", 0, 1)
    assert sev == "HIGH"


def test_classify_severity_swagger() -> None:
    sev, kind = classify_severity("SwaggerSchema", "content_hash", 0, 0)
    assert sev == "HIGH"
    assert kind == "API_SCHEMA_CHANGE"
    

def test_classify_severity_symbol() -> None:
    sev, kind = classify_severity("Symbol", "signature", 6, 2)
    assert sev == "HIGH"
    assert kind == "SIGNATURE_CHANGE"
    
    sev, kind = classify_severity("Symbol", "signature", 3, 1)
    assert sev == "MEDIUM"

    sev, kind = classify_severity("Symbol", "signature", 0, 0)
    assert sev == "LOW"
    

def test_classify_severity_visibility() -> None:
    sev, kind = classify_severity("Symbol", "visibility", 0, 0)
    assert sev == "LOW"
    assert kind == "VISIBILITY_REDUCED"


@pytest.fixture(autouse=True)
def mock_db():
    with patch("axon.breaking.detector.async_session") as m_async:
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
            if "pv.changed_at_commit = $sha" in query:
                return MockResult(data=[{
                    "node_id": "ep1",
                    "node_label": "ApiEndpoint",
                    "property_name": "schema_hash",
                    "old_value": "A",
                    "new_value": "B"
                }])
            elif "consumer IS NOT NULL" in query:
                return MockResult(data=[
                    {"team": "payments", "target": "//a:b"},
                    {"team": "checkout", "target": "//c:d"}
                ])
            elif "MERGE (a:BreakingChangeAlert" in query:
                return MockResult(single={"a": {"id": kwargs.get("id"), "severity": kwargs.get("severity")}})
            return MockResult()
            
        session_mock.run.side_effect = mock_run
        yield m_async


@pytest.mark.asyncio
async def test_detect_and_write() -> None:
    alerts = await detect_and_write("abc1234", "author@co.com")
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "CRITICAL"
    assert alerts[0]["id"] == "alert::abc1234::ep1::schema_hash"
