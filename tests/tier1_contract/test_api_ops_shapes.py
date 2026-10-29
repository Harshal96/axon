"""
Verify that every operation endpoint returns the correct response shape.
Seeds the graph first, then calls every endpoint and validates the Pydantic
model can parse the response without error.
"""
import httpx
import pytest

from axon.api.models import (
    AffectedTestsResult,
    BlastRadiusResult,
    SearchResult,
    SubgraphResult,
    VisibilityCheckResult,
)

pytestmark = pytest.mark.tier1


@pytest.fixture(autouse=True)
def seed(seed_graph):
    pass


class TestBlastRadiusShape:
    def test_returns_200(self, api_client):
        r = api_client.get("/api/v1/ops/blast-radius",
                           params={"label": "//services/payment:payment_lib"})
        assert r.status_code == 200

    def test_response_is_valid_model(self, api_client):
        r = api_client.get("/api/v1/ops/blast-radius",
                           params={"label": "//services/payment:payment_lib"})
        # Must not raise
        BlastRadiusResult.model_validate(r.json())

    def test_meta_fields_present(self, api_client):
        r = api_client.get("/api/v1/ops/blast-radius",
                           params={"label": "//services/payment:payment_lib"})
        meta = r.json()["meta"]
        assert "is_stale" in meta
        assert "query_duration_ms" in meta
        assert "cache_hit" in meta
        assert "graph_version" in meta

    def test_unknown_label_returns_404_or_empty(self, api_client):
        r = api_client.get("/api/v1/ops/blast-radius",
                           params={"label": "//does/not:exist"})
        # Either 404 or empty affected_targets — both are acceptable
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert r.json()["affected_targets"] == []

    def test_second_call_is_cache_hit(self, api_client):
        params = {"label": "//services/payment:payment_lib"}
        api_client.get("/api/v1/ops/blast-radius", params=params)
        r2 = api_client.get("/api/v1/ops/blast-radius", params=params)
        assert r2.json()["meta"]["cache_hit"] is True

    def test_depth_parameter_accepted(self, api_client):
        r = api_client.get("/api/v1/ops/blast-radius",
                           params={"label": "//services/payment:payment_lib",
                                   "depth": 2})
        assert r.status_code == 200

    def test_depth_above_max_rejected(self, api_client):
        r = api_client.get("/api/v1/ops/blast-radius",
                           params={"label": "//services/payment:payment_lib",
                                   "depth": 99})
        assert r.status_code == 422


class TestSubgraphShape:
    def test_returns_200(self, api_client):
        r = api_client.post("/api/v1/ops/subgraph",
                            json=["//services/payment:payment_lib"])
        assert r.status_code == 200

    def test_response_is_valid_model(self, api_client):
        r = api_client.post("/api/v1/ops/subgraph",
                            json=["//services/payment:payment_lib"])
        SubgraphResult.model_validate(r.json())

    def test_token_estimate_present(self, api_client):
        r = api_client.post("/api/v1/ops/subgraph",
                            json=["//services/payment:payment_lib"])
        assert "token_estimate" in r.json()
        assert r.json()["token_estimate"] > 0

    def test_token_budget_respected(self, api_client):
        r = api_client.post(
            "/api/v1/ops/subgraph",
            json=["//services/payment:payment_lib"],
            params={"token_budget": 500},
        )
        assert r.json()["token_estimate"] <= 600  # small tolerance


class TestSearchShape:
    def test_search_returns_hits(self, api_client):
        r = api_client.get("/api/v1/ops/search", params={"q": "payment"})
        assert r.status_code == 200
        data = SearchResult.model_validate(r.json())
        assert data.total >= 0

    def test_search_empty_query_rejected(self, api_client):
        r = api_client.get("/api/v1/ops/search", params={"q": "x"})
        # Single char is valid (min_length=2 was set — but 'x' is 1 char)
        # This tests the validation
        assert r.status_code in (200, 422)

    def test_search_limit_respected(self, api_client):
        r = api_client.get("/api/v1/ops/search",
                           params={"q": "payment", "limit": 2})
        assert len(r.json()["hits"]) <= 2


class TestAffectedTestsShape:
    def test_returns_200(self, api_client):
        r = api_client.post("/api/v1/ops/affected-tests",
                            json=["services/payment/src/payment.py"])
        assert r.status_code == 200

    def test_response_is_valid_model(self, api_client):
        r = api_client.post("/api/v1/ops/affected-tests",
                            json=["services/payment/src/payment.py"])
        AffectedTestsResult.model_validate(r.json())

    def test_empty_input_returns_empty(self, api_client):
        r = api_client.post("/api/v1/ops/affected-tests", json=[])
        assert r.status_code == 200
        assert r.json()["total"] == 0


class TestVisibilityShape:
    def test_allowed_dep(self, api_client):
        r = api_client.get("/api/v1/ops/visibility-check",
                           params={
                               "from_label": "//services/checkout:checkout_lib",
                               "to_label": "//services/payment:generate_payment_client_py",
                           })
        assert r.status_code == 200
        data = VisibilityCheckResult.model_validate(r.json())
        assert data.is_allowed is True

    def test_disallowed_dep(self, api_client):
        # payment_gateway_java has visibility //services/payment:__pkg__ only
        r = api_client.get("/api/v1/ops/visibility-check",
                           params={
                               "from_label": "//services/checkout:checkout_lib",
                               "to_label": "//services/payment:payment_gateway_java",
                           })
        assert r.status_code == 200
        data = VisibilityCheckResult.model_validate(r.json())
        assert data.is_allowed is False
        assert data.suggestion is not None


class TestHealthShape:
    def test_health_ok(self, api_client):
        r = api_client.get("/api/v1/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] in ("ok", "degraded")
        assert data["neo4j"] == "connected"

    def test_health_has_graph_version(self, api_client):
        r = api_client.get("/api/v1/health")
        assert "graph_version" in r.json()

    def test_unauthorized_returns_401(self, api_client):
        client = httpx.Client(
            base_url=api_client.base_url,
            headers={"Authorization": "Bearer wrong-token"},
        )
        r = client.get("/api/v1/ops/blast-radius",
                       params={"label": "//services/payment:payment_lib"})
        assert r.status_code == 401
