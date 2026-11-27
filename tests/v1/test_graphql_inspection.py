from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from axon.api.app import create_app
from axon.v1.ask import ask_deterministic
from axon.v1.impact import compute_impact
from axon.v1.indexer import index_repo
from axon.v1.models import endpoint_node_id, graphql_endpoint_node_id
from axon.v1.scoped_index import resolve_endpoint
from axon.v1.store import MemoryStore, save_snapshot_file
from cli.app import app as cli_app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/evals/typescript-apollo"
REPO = "eval-typescript-apollo"
FIELD = graphql_endpoint_node_id(REPO, "orders", "Query.order")
ARCHIVE = graphql_endpoint_node_id(REPO, "archive", "Query.order")
PAGE = endpoint_node_id(REPO, "shop", "GET", "/orders/{id}")


@pytest.fixture(scope="module")
def snapshot():
    return index_repo(FIXTURE, use_cache=False, workers=1)[0]


def test_ask_and_scoped_references_use_exact_coordinate_and_service(snapshot):
    answer = ask_deterministic(snapshot, "Who calls Query.order?")
    assert answer.intent == "ambiguous"
    assert {c.node_id for c in answer.citations} == {FIELD, ARCHIVE}
    answer = ask_deterministic(snapshot, "Callers of Query.order in `orders`?")
    assert answer.intent == "callers"
    assert "orderPage" in answer.answer
    assert answer.citations[0].label == "Query.order"
    assert answer.citations[0].file == "orders/orders.graphql"
    assert answer.citations[0].line == 2
    assert ask_deterministic(snapshot, "Who calls query.order?").intent == "unknown"
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_endpoint(snapshot, "Query.order", repo=REPO)
    assert resolve_endpoint(snapshot, "Query.order", "archive", REPO).id == ARCHIVE
    with pytest.raises(ValueError, match="outside requested scope"):
        resolve_endpoint(snapshot, FIELD, "archive", REPO)


def test_catalog_and_detail_expose_resolver_and_executed_call_sites(snapshot):
    app = create_app()
    app.state.v1_store = MemoryStore(snapshot)
    client = TestClient(app)
    (field,) = client.get("/api/v1/apis", params={"service": "orders", "q": "query.order"}).json()
    assert field["label"] == "Query.order"
    assert "loadOrder" in field["resolver"]
    detail = client.get(f"/api/v1/apis/{FIELD}").json()
    assert detail["schema_edges"][0]["evidence"]["detail"] == "order(id: ID!): Order"
    assert detail["caller_edges"][0]["props"]["operation_name"] == "OrderPage"
    for path, body in [
        ("preview", {"prompt": "change the resolver"}),
        ("apply", {"handler": "loadOrder"}),
    ]:
        response = client.post(f"/api/v1/apis/{FIELD}/corrections/{path}", json=body)
        assert response.status_code == 400 and "HTTP endpoint" in response.json()["detail"]
    doc = client.get(
        f"/api/v1/repos/{REPO}/api-explain", params={"api": "Query.order", "service": "orders"}
    ).json()
    assert doc["endpoint"]["id"] == FIELD


def test_source_impact_distinguishes_document_resolver_and_unused_schema(snapshot):
    fragment = compute_impact(snapshot, ["shop/fragment.ts"], repo_id=REPO)
    assert fragment.direct_apis == [PAGE]
    resolver = compute_impact(snapshot, ["orders/orderResolver.ts"], repo_id=REPO)
    assert resolver.direct_apis == [FIELD]
    assert set(resolver.affected_apis) == {"Query.order (orders)", "GET /orders/{id} (shop)"}
    declaration = compute_impact(snapshot, ["archive/schema.gql"], repo_id=REPO)
    assert ARCHIVE in declaration.direct_apis and FIELD not in declaration.direct_apis
    assert not compute_impact(snapshot, ["shop/schema-copy.graphql"], repo_id=REPO).direct_apis


def test_cli_prints_coordinate_and_guards_http_corrections(snapshot, tmp_path):
    graph = tmp_path / "snapshot.json"
    save_snapshot_file(snapshot, graph)
    runner = CliRunner()
    listing = runner.invoke(cli_app, ["apis", "--snapshot", str(graph)], env={"COLUMNS": "180"})
    assert listing.exit_code == 0 and "Query.order" in listing.output
    (tmp_path / "axon.yaml").write_text("repo: demo\nservices: [{id: orders, roots: [orders]}]\n")
    correction = runner.invoke(
        cli_app,
        [
            "handler",
            "set",
            "--repo",
            str(tmp_path),
            "--service",
            "orders",
            "--api",
            "Query.order",
            "--handler",
            "loadOrder",
            "--apply",
        ],
    )
    assert correction.exit_code == 2 and "HTTP reference" in correction.output
