"""Publication and consumer regressions from the final multi-repository review."""

import pytest
import yaml

from axon.v1.diagnose import link_diagnosis, matching_status
from axon.v1.facts import FileFacts, HttpFact
from axon.v1.graph import GraphSnapshot
from axon.v1.models import EdgeKind, GraphNode, NodeKind, endpoint_node_id, service_node_id
from axon.v1.repository_links import prepare_link_inputs
from axon.v1.store import MemoryStore
from tests.v1.test_multirepo_linking import caller, outbound, repository


@pytest.mark.parametrize("missing", ["repo", "service", "endpoint"])
@pytest.mark.parametrize("identity", [True, False])
def test_authoritative_client_survives_local_consensus_and_target_churn(missing, identity):
    snap = caller(explicit_base=False)
    sm = snap.link_inputs["A"].service_map
    sm.services[0].calls.clear()
    for service, path in [
        ("orders", "/one"),
        ("orders", "/two"),
        ("orders", "/entries"),
        ("other", "/entries"),
    ]:
        snap.add_node(
            GraphNode(
                id=service_node_id("A", service),
                kind=NodeKind.SERVICE,
                repo_id="A",
                service_id=service,
            )
        )
        snap.add_node(
            GraphNode(
                id=endpoint_node_id("A", service, "GET", path),
                kind=NodeKind.API_ENDPOINT,
                repo_id="A",
                service_id=service,
                props={"method": "GET", "path": path},
            )
        )
    facts = FileFacts(
        "orders/api.py",
        "python",
        http_calls=[
            HttpFact("handle", 10, "GET", "{env:BASE}/one", "requests"),
            HttpFact("handle", 20, "GET", "{env:BASE}/two", "requests"),
            HttpFact(
                "handle",
                30,
                "GET",
                "{env:BASE}/entries",
                "generated-source",
                call_name="ledger_sdk.getEntries",
                client_id="ledger-client" if identity else "",
                client_owner="orders" if identity else "",
            ),
        ],
    )
    snap.link_inputs["A"] = prepare_link_inputs(snap, sm, [facts], {})
    store = MemoryStore()
    if missing != "repo":
        store.publish_repository(
            repository("B", "other" if missing == "service" else "ledger", endpoint=False),
            repo_id="B",
        )
    store.publish_repository(snap, repo_id="A")

    def unresolved():
        current = store.load()
        edge = outbound(current)[30]
        assert current.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API
        assert edge.props["target_repo"] == "B"
        assert edge.props["target_service"] == "ledger"
        assert outbound(current)[10].to_id == endpoint_node_id("A", "orders", "GET", "/one")

    unresolved()
    store = MemoryStore(GraphSnapshot.model_validate_json(store.load().model_dump_json()))
    store.publish_repository(repository("C", "ledger"), repo_id="C")
    unresolved()
    store.publish_repository(repository("B", "ledger"), repo_id="B")
    assert outbound(store.load())[30].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")
    store.remove_repository("B")
    unresolved()


@pytest.mark.parametrize("legacy", [True, False])
def test_zero_site_origin_survives_json_private_codec_and_target_replay(legacy):
    from axon.v1.ask import ask_deterministic
    from axon.v1.impact import compute_impact
    from axon.v1.incremental import publication_result
    from axon.v1.link_publication import decode_link_inputs, encode_link_inputs

    a = repository("A", "orders")
    if legacy:
        a.schema_version = 1
        a.link_inputs.clear()
    store = MemoryStore(a)
    store.publish_repository(repository("B", "ledger"), repo_id="B")
    snap = GraphSnapshot.model_validate_json(store.load().model_dump_json())
    snap.link_inputs = decode_link_inputs(
        encode_link_inputs(snap.link_inputs, owner="test", generation="g")
    )
    store = MemoryStore(snap)
    store.publish_repository(repository("B", "ledger", endpoint=False), repo_id="B")
    snap = store.load()
    assert matching_status(snap, "A") == {"pinned_legacy": 0, "fresh_matching_unknown": legacy}
    assert snap.resolution_report("A")["orders"]["fresh_matching_unknown"] is legacy
    assert ("A" in publication_result(snap)["link_inputs_missing"]) is legacy
    assert (
        any(
            "A: link-inputs-missing" in s
            for s in ask_deterministic(snap, "Which APIs are in A::orders?").blind_spots
        )
        is legacy
    )
    assert (
        any(
            "Re-index A" in s
            for s in compute_impact(snap, ["ledger/api.py"], repo_id="B").limitations
        )
        is legacy
    )
    assert not snap.link_inputs["A"].pinned
    store.publish_repository(repository("A", "orders"), repo_id="A")
    assert not matching_status(store.load(), "A")["fresh_matching_unknown"]


@pytest.mark.parametrize("missing", ["repo", "service", "endpoint"])
def test_declared_call_scope_matches_diagnosis_preview_and_replay(tmp_path, missing):
    from axon.v1.api_fix import CorrectionRequest, SuppressInput, build_plan
    from axon.v1.link_publication import decode_link_inputs, encode_link_inputs
    from axon.v1.servicemap import Suppression

    a = caller()
    a.link_inputs["A"].prepared.clear()
    store = MemoryStore()
    store.publish_repository(a, repo_id="A")
    if missing != "repo":
        store.publish_repository(
            repository("B", "other" if missing == "service" else "ledger", endpoint=False),
            repo_id="B",
        )
    snap = GraphSnapshot.model_validate_json(store.load().model_dump_json())
    snap.link_inputs = decode_link_inputs(
        encode_link_inputs(snap.link_inputs, owner="test", generation="g")
    )
    store = MemoryStore(snap)
    store.publish_repository(repository("C", "ledger"), repo_id="C")
    snap = store.load()
    edge = outbound(snap)[30]
    assert snap.nodes[edge.to_id].repo_id == ""
    diagnosis = link_diagnosis(snap, edge)
    assert diagnosis["reason"] == f"target-{missing}-missing"
    assert (diagnosis["target_repo"], diagnosis["target_service"]) == ("B", "ledger")
    (tmp_path / "axon.yaml").write_text(
        yaml.safe_dump(a.link_inputs["A"].service_map.model_dump(mode="json"))
    )
    _, _, plan = build_plan(
        tmp_path,
        snap,
        endpoint_node_id("A", "orders", "GET", "/orders"),
        CorrectionRequest(
            suppress=[SuppressInput(to_repo="B", to_service="ledger", reason="wrong call")]
        ),
    )
    effect = next(e for e in plan["effects"] if e["kind"] == "SUPPRESS")
    assert effect["matching_edges_now"] == 1
    a.link_inputs["A"].service_map.services[0].suppress.append(
        Suppression(
            id="review",
            from_api="GET /orders",
            to_repo="B",
            to_service="ledger",
            reason="wrong call",
        )
    )
    store = MemoryStore(snap)
    store.publish_repository(a, repo_id="A")
    assert not [e for e in store.load().edges if e.kind == EdgeKind.CALLS_API]


def test_structural_diagnostics_replace_remove_and_keep_other_repo_same_file():
    from axon.v1.ask import ask_deterministic
    from axon.v1.link_publication import decode_link_inputs, encode_link_inputs

    a, b = repository("A", "orders"), repository("B", "orders")
    warning = "orders/api.py:8: untyped client: dynamic receiver"
    a.diagnostics = [warning]
    b.diagnostics = [warning, "orders/api.py:9: factory package: B only"]
    store = MemoryStore()
    store.publish_repository(a, repo_id="A")
    store.publish_repository(b, repo_id="B")
    snap = GraphSnapshot.model_validate_json(store.load().model_dump_json())
    snap.link_inputs = decode_link_inputs(
        encode_link_inputs(snap.link_inputs, owner="test", generation="g")
    )
    store = MemoryStore(snap)
    store.publish_repository(repository("A", "orders"), repo_id="A")
    assert warning in store.load().diagnostics  # B still owns an identical warning.
    assert not any(
        "Unlinked client evidence" in s
        for s in ask_deterministic(store.load(), "What does A::orders call?").blind_spots
    )
    assert any(
        "Unlinked client evidence" in s
        for s in ask_deterministic(store.load(), "What does B::orders call?").blind_spots
    )
    store.remove_repository("B")
    assert not store.load().diagnostics
    store.remove_repository("A")
    assert not store.load().diagnostics


def test_legacy_zero_pin_doctor_and_same_commit_require_caller_preparation(tmp_path, monkeypatch):
    from axon.v1.api import _already_indexed
    from axon.v1.doctor import doctor
    from axon.v1.fingerprints import INDEXER_FINGERPRINT
    from axon.v1.servicemap import load_service_map, service_map_fingerprint

    (tmp_path / "axon.yaml").write_text("repo: A\nservices:\n  - id: orders\n    roots: [orders]\n")
    a = repository("A", "orders")
    a.link_inputs.clear()
    node = a.nodes["repo::A"]
    node.props.update(
        {
            "commit": "head",
            "axon_build": INDEXER_FINGERPRINT,
            "source_path": str(tmp_path.resolve()),
            "service_map_hash": service_map_fingerprint(tmp_path),
            "dirty": "false",
            "index_options": load_service_map(tmp_path).index.fingerprint(),
        }
    )
    monkeypatch.setattr("axon.v1.revision.checkout_dirty", lambda root: False)
    store = MemoryStore(a)
    store.publish_repository(repository("B", "ledger"), repo_id="B")
    assert _already_indexed(store, tmp_path, "head") is None
    assert doctor(tmp_path, snapshot=store.load())["fresh_matching_unknown"] is True
    complete = repository("A", "orders")
    complete.nodes["repo::A"] = node
    store.publish_repository(complete, repo_id="A")
    assert _already_indexed(store, tmp_path, "head")["already_indexed"] is True


def test_source_warning_adoption_stages_inputs_without_replacing_source(monkeypatch):
    from axon.v1.link_publication import decode_link_inputs
    from axon.v1.store import Neo4jStore
    from tests.v1.test_store import _FakeSession

    current = repository("A", "orders")
    current.diagnostics = ["orders/api.py:8: untyped client: retained A"]
    current.link_inputs["A"].source_generation = "source-A"
    writer = Neo4jStore()
    writer._cached = current
    writer._physical_ids = {key: "source-A:" + key for key in current.nodes}
    writer._generations = {"A": "source-A"}
    session = _FakeSession()
    monkeypatch.setattr(writer, "_session", lambda: session)
    monkeypatch.setattr(
        writer,
        "stored_publications",
        lambda: [
            {
                "repo_id": "A",
                "generation": "source-A",
                "input_generation": "inputs-A",
                "prev_generation": "retained-A",
                "prev_input_generation": "retained-inputs-A",
                "schema_version": 2,
            }
        ],
    )
    writer.publish_repository(repository("B", "ledger"), repo_id="B")
    staged = [
        row
        for query, kwargs in session.calls
        if "CREATE (x:_AxonLinkInput)" in query
        for row in kwargs["rows"]
        if row["key"] == "A"
    ]
    restored = decode_link_inputs(staged)
    assert restored["A"].structural_diagnostics == current.diagnostics
    assert restored["A"].source_generation == "source-A"
    markers = next(
        kwargs["rows"][0]["markers"]
        for query, kwargs in session.calls
        if "SET p.generation" in query
    )
    marker = next(m for m in markers if m["repo_id"] == "A")
    assert marker["generation"] == "source-A"
    assert marker["input_generation"] != "inputs-A"
    assert marker["prev_generation"] == "retained-A"
    assert marker["prev_input_generation"] == "retained-inputs-A"
