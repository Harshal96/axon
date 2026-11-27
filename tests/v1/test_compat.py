"""Frozen-format compatibility tests.

Rule: a committed fixture under tests/fixtures/compat/ is never edited.
A graph schema bump adds a new fixture set and keeps the old ones loading
(or failing with the actionable GraphSchemaError) here.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from axon.v1.graph import GraphSnapshot
from axon.v1.models import GRAPH_SCHEMA_VERSION, GraphEdge, GraphNode, GraphSchemaError, NodeKind
from axon.v1.store import load_snapshot_file

COMPAT = Path(__file__).resolve().parents[1] / "fixtures" / "compat"


def test_frozen_snapshot_v1_loads():
    snap = load_snapshot_file(COMPAT / "snapshot-v1.json")
    assert snap.schema_version == 1
    assert snap.nodes
    assert snap.by_kind(NodeKind.API_ENDPOINT)


def test_frozen_state_v1_loads(tmp_path):
    from axon.v1.indexstate import StateStore

    # StateStore lays out <dir>/<repo>/state.db; stage the frozen file there.
    staged = tmp_path / "fixture-monorepo"
    staged.mkdir()
    shutil.copyfile(COMPAT / "state-v1.db", staged / "state.db")
    store = StateStore("fixture-monorepo", tmp_path)
    loaded = store.load()
    assert loaded is not None
    assert loaded.commit == "compat-v1"
    assert loaded.snapshot.schema_version == 1
    for version in (1, 2):
        published = load_snapshot_file(COMPAT / f"snapshot-v{version}.json")
        loaded = store.load(published)
        assert loaded is not None and loaded.snapshot.schema_version == version


@pytest.mark.parametrize("version", [1, 2])
def test_frozen_neo4j_rows_hydrate(version):
    from axon.v1.store import Neo4jStore
    from tests.v1.test_store import _FakeResult, _FakeSession

    rows = json.loads((COMPAT / f"neo4j-rows-v{version}.json").read_text())

    class PublicationSession(_FakeSession):
        def run(self, query, **kwargs):
            if query.startswith("MATCH (p:_AxonPublication)"):
                return _FakeResult(rows["publications"])
            return super().run(query, **kwargs)

    session = PublicationSession(
        node_rows=rows["nodes"],
        edge_rows=[
            {"a": e["from_id"], "b": e["to_id"], "kind": e["kind"], "props": e["props"]}
            for e in rows["edges"]
        ],
    )
    store = Neo4jStore()
    store._session = lambda: session  # type: ignore[method-assign]
    snap = store.load()
    assert snap.schema_version == version
    if version == 1:
        assert "service::fixture-monorepo::legacy" in snap.nodes
    else:
        assert snap.model_dump() == load_snapshot_file(COMPAT / "snapshot-v2.json").model_dump()


def test_rebuild_v1_snapshot_with_mixed_protocols():
    from axon.v1.graph_delta import GraphDelta
    from axon.v1.models import EdgeKind, endpoint_label
    from axon.v1.store import MemoryStore

    old = load_snapshot_file(COMPAT / "snapshot-v1.json")
    mixed = load_snapshot_file(COMPAT / "snapshot-v2.json")
    store = MemoryStore(old)
    store.save(mixed)
    assert store.load().model_dump() == mixed.model_dump()
    delta = GraphDelta.between(old, mixed)
    assert delta.apply(old).model_dump() == mixed.model_dump()
    assert old.schema_version == 1
    assert {endpoint_label(ep) for ep in mixed.by_kind(NodeKind.API_ENDPOINT)} == {
        "POST /graphql",
        "Query.order",
    }
    calls = [edge for edge in mixed.edges if edge.kind == EdgeKind.CALLS_API]
    mixed.add_edge(calls[0].model_copy(deep=True))
    mixed.dedupe_edges()
    assert [edge for edge in mixed.edges if edge.kind == EdgeKind.CALLS_API] == calls
    assert [edge.props["column"] for edge in calls] == ["3", "49"]
    view = mixed.api_graph(calls[0].to_id)
    visible = [edge for edge in view["edges"] if edge["kind"] == "CALLS_API"]
    assert len(visible) == 1
    assert visible[0]["props"] == calls[0].props
    assert visible[0]["evidence"] == calls[0].evidence.model_dump()


def test_http_resolution_and_manual_overrides_ignore_graphql_fields():
    from axon.v1.facts import HttpFact
    from axon.v1.linker import EndpointIndex, resolve_http
    from axon.v1.models import endpoint_node_id, graphql_endpoint_node_id
    from axon.v1.servicemap import ServiceMap

    mixed = load_snapshot_file(COMPAT / "snapshot-v2.json")
    repo = "fixture-monorepo"
    field = mixed.nodes[graphql_endpoint_node_id(repo, "orders", "Query.order")]
    for service, coordinate in [("other", "Query.order"), ("orders", "Mutation.order")]:
        mixed.add_node(
            field.model_copy(
                update={
                    "id": graphql_endpoint_node_id(repo, service, coordinate),
                    "service_id": service,
                    "name": coordinate,
                    "props": {
                        **field.props,
                        "coordinate": coordinate,
                        "root_type": coordinate.split(".")[0],
                    },
                }
            )
        )
    by_coordinate = mixed.snapshot_index().graphql_by_coordinate
    assert {n.service_id for n in by_coordinate["Query.order"]} == {"orders", "other"}
    assert "query.order" not in by_coordinate
    index = EndpointIndex(mixed)
    assert index.by_operation("order") == []
    http_id = endpoint_node_id(repo, "orders", "POST", "/graphql")
    assert [ep.id for ep in index.by_method_path("ANY", "/graphql")] == [http_id]
    sm = ServiceMap.model_validate(
        {
            "repo": repo,
            "links": [
                {
                    "call_name": "order",
                    "to_service": "orders",
                    "to_method": "POST",
                    "to_path": "/graphql",
                }
            ],
        }
    )
    match = resolve_http(HttpFact(caller="page", line=8, call_name="order"), index, sm, repo)
    assert match and match[0] == http_id


def test_snapshot_too_new_fails_with_action(tmp_path):
    data = json.loads((COMPAT / "snapshot-v1.json").read_text())
    data["schema_version"] = 99
    target = tmp_path / "new.json"
    target.write_text(json.dumps(data))
    with pytest.raises(GraphSchemaError, match="Upgrade Axon"):
        load_snapshot_file(target)


def test_snapshot_unknown_kind_fails_with_action(tmp_path):
    target = tmp_path / "old.json"
    target.write_text(json.dumps({"nodes": {"a": {"id": "a", "kind": "OldKind"}}, "edges": []}))
    with pytest.raises(GraphSchemaError, match="Re-index"):
        load_snapshot_file(target)


def test_servicemap_future_version_rejected():
    from axon.v1.servicemap import ServiceMap

    with pytest.raises(ValueError, match="newer than this Axon supports"):
        ServiceMap.model_validate({"version": 99, "services": []})
    assert ServiceMap.model_validate({"version": 1, "future_key": 1}).version == 1


def test_health_reports_versions():
    from fastapi.testclient import TestClient

    from axon.api.app import create_app

    health = TestClient(create_app()).get("/api/v1/health").json()
    assert health["graph_schema_version"] == GRAPH_SCHEMA_VERSION
    assert health["axon_version"]
    assert health["stored_graph_schema_version"] == GRAPH_SCHEMA_VERSION


# -- pinned contracts -------------------------------------------------------


def test_node_id_formats_are_pinned():
    from axon.v1.models import (
        client_node_id,
        endpoint_node_id,
        external_node_id,
        file_node_id,
        graphql_endpoint_node_id,
        graphql_external_node_id,
        repo_node_id,
        service_node_id,
        spec_node_id,
        symbol_node_id,
    )

    assert repo_node_id("r") == "repo::r"
    assert service_node_id("r", "s") == "service::r::s"
    assert file_node_id("r", "a/b.py") == "file::r::a/b.py"
    assert symbol_node_id("r", "a/b.py", "M.f") == "symbol::r::a/b.py::M.f"
    assert endpoint_node_id("r", "s", "get", "/x") == "endpoint::r::s::GET:/x"
    assert (
        graphql_endpoint_node_id("r", "s", "Query.order")
        == "endpoint::r::s::graphql::default::Query.order"
    )
    assert (
        graphql_external_node_id("r", "caller", 7, 3, "orders")
        == "external::graphql::r::orders::caller::7:3"
    )
    assert spec_node_id("r", "openapi.yaml") == "spec::r::openapi.yaml"
    assert client_node_id("r", "c") == "client::r::c"
    assert external_node_id("post", "https://x/{id}") == "external::POST:https://x/{id}"


def test_props_keys_by_kind_are_pinned(fixtures):
    from axon.v1.indexer import index_repo

    snap, _ = index_repo(str(fixtures / "monorepo"), use_cache=False)
    actual: dict[str, set[str]] = {}
    for node in snap.nodes.values():
        actual.setdefault(node.kind.value, set()).update(node.props)
    expected = {
        kind: set(keys)
        for kind, keys in json.loads(
            Path(__file__).with_name("props_by_kind.json").read_text()
        ).items()
    }
    assert actual == expected


def test_api_response_field_sets_are_pinned(monorepo_snap):
    from fastapi.testclient import TestClient

    from axon.api.app import create_app

    app = create_app()
    app.state.v1_store.save(monorepo_snap)
    client = TestClient(app)
    services = client.get("/api/v1/services?repo=fixture-monorepo").json()
    assert services and {"id", "kind", "name", "repo_id", "service_id", "file", "props"} <= set(
        services[0]
    )
    apis = client.get("/api/v1/apis?repo=fixture-monorepo").json()
    assert apis
    detail = client.get(f"/api/v1/apis/{apis[0]['id']}").json()
    assert {"endpoint", "handler", "handlers", "outbound", "callers"} <= set(detail)


# -- round trips -------------------------------------------------------------


@settings(max_examples=25, deadline=None)
@given(
    name=st.text(alphabet="abcdefghij", min_size=1, max_size=6),
    path=st.text(alphabet="abc/", min_size=1, max_size=8),
)
def test_snapshot_round_trip_property(name, path):
    snap = GraphSnapshot(
        nodes={
            "a": GraphNode(id="a", kind=NodeKind.SYMBOL, name=name, repo_id="r", file=path),
            "b": GraphNode(id="b", kind=NodeKind.SYMBOL, name="b", repo_id="r"),
        },
        edges=[GraphEdge(from_id="a", to_id="b", kind="CALLS")],
    )
    loaded = GraphSnapshot.model_validate_json(snap.model_dump_json())
    assert loaded.schema_version == GRAPH_SCHEMA_VERSION
    assert loaded.nodes["a"].name == name
    assert loaded.edges[0].kind.value == "CALLS"


def test_facts_codec_tolerates_missing_fields():
    from axon.v1.facts import facts_from_dict

    facts = facts_from_dict({"path": "a.py", "language": "python"})
    assert facts.path == "a.py"
    assert facts.symbols == []
