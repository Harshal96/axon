import json

import pytest

from axon.v1.ask import ask_deterministic
from axon.v1.impact import compute_impact
from axon.v1.models import EdgeKind, Evidence, GraphEdge, GraphNode, NodeKind, symbol_node_id
from tests.v1.test_ask_entities import qualified_snapshot


@pytest.mark.parametrize(
    "phrasing,expected",
    [
        ("What does GET /run in A::orders call?", {"A", "B"}),
        ("What does GET /run in A::orders call transitively?", {"A", "B", "C"}),
        ("What downstream APIs does GET /run in A::orders call?", {"A", "B", "C"}),
    ],
)
def test_direct_endpoint_calls_require_explicit_transitivity(phrasing, expected):
    snap = qualified_snapshot()
    snap.edges = [edge for edge in snap.edges if edge.kind != EdgeKind.CALLS_API]
    for source, repo in [("A-handler", "B"), ("B-handler", "C")]:
        snap.add_edge(
            GraphEdge(
                from_id=source, to_id=f"endpoint::{repo}::ledger::GET:/run", kind=EdgeKind.CALLS_API
            )
        )

    response = ask_deterministic(snap, phrasing)

    assert response is not None and response.intent == "outbound"
    assert {c.repo_id for c in response.citations} == expected


def test_qualified_dotted_service_names_and_multi_service_listings():
    snap = qualified_snapshot()
    for kind, node_id in [
        (NodeKind.SERVICE, "service::B::ledger.v2"),
        (NodeKind.API_ENDPOINT, "endpoint::B::ledger.v2::GET:/run"),
    ]:
        snap.add_node(
            GraphNode(
                id=node_id,
                kind=kind,
                repo_id="B",
                service_id="ledger.v2",
                props={"method": "GET", "path": "/run"} if kind == NodeKind.API_ENDPOINT else {},
            )
        )

    response = ask_deterministic(snap, "List APIs in B::ledger.v2 and C::ledger.")

    assert response is not None and response.intent == "list_apis"
    assert {c.node_id for c in response.citations} == {
        "endpoint::B::ledger.v2::GET:/run",
        "endpoint::C::ledger::GET:/run",
    }
    missing = ask_deterministic(snap, "List APIs in B::ledger.v3.")
    assert missing is not None and missing.intent == "unknown"


def test_exact_graph_id_ask_targets_only_selected_foreign_endpoint():
    snap = qualified_snapshot()
    endpoint = "endpoint::B::ledger::GET:/run"
    response = ask_deterministic(snap, f"Who calls {json.dumps(endpoint)}?")

    assert response is not None and response.intent == "callers"
    assert [c.node_id for c in response.citations] == [endpoint, "A-handler"]
    missing = ask_deterministic(snap, 'Who calls "endpoint::D::ledger::GET:/run"?')
    assert missing is not None and missing.intent == "unknown"


def test_exact_graph_id_selects_symbol_among_same_names():
    snap = qualified_snapshot()
    symbol = symbol_node_id("B", "app/my worker.py", "handle")
    for repo in ("B", "C"):
        sid = symbol_node_id(repo, "app/my worker.py", "handle")
        snap.add_node(GraphNode(id=sid, kind=NodeKind.SYMBOL, repo_id=repo, name="handle"))
        snap.add_edge(
            GraphEdge(
                from_id=sid, to_id=f"endpoint::{repo}::ledger::GET:/run", kind=EdgeKind.HANDLES
            )
        )

    response = ask_deterministic(snap, f"Which APIs include the function {json.dumps(symbol)}?")

    assert response is not None and response.intent == "function_apis"
    assert [row.source.repo_id for row in response.rows] == ["B"]


def test_intent_words_inside_qualified_ids_are_not_command_words():
    snap = qualified_snapshot()
    snap.add_node(
        GraphNode(id="service::A::call", kind=NodeKind.SERVICE, repo_id="A", service_id="call")
    )
    snap.add_node(GraphNode(id="call-worker", kind=NodeKind.SYMBOL, repo_id="A", service_id="call"))
    snap.add_edge(
        GraphEdge(
            from_id="call-worker", to_id="endpoint::B::ledger::GET:/run", kind=EdgeKind.CALLS_API
        )
    )
    question = 'What services does "service::A::call" call?'
    response = ask_deterministic(snap, question)

    assert response is not None and response.intent == "service_outbound"
    assert len(response.rows) == 1


def test_endpoint_ask_counts_caller_symbols_once_with_both_sites():
    snap = qualified_snapshot()
    endpoint = "endpoint::B::ledger::GET:/run"
    snap.add_edge(
        GraphEdge(
            from_id="A-handler",
            to_id=endpoint,
            kind=EdgeKind.CALLS_API,
            evidence=Evidence(file="app/main.py", line=44, rule="base-url:ledger"),
        )
    )

    response = ask_deterministic(snap, f"Who calls {json.dumps(endpoint)}?")

    assert response is not None and "1 in-graph caller(s)" in response.answer
    assert [cite.node_id for cite in response.citations] == [endpoint, "A-handler"]
    assert all(line in response.answer for line in ("app/main.py:2", "app/main.py:44"))


@pytest.fixture
def worker_snapshot():
    snap = qualified_snapshot()
    snap.edges = [edge for edge in snap.edges if edge.kind != EdgeKind.CALLS_API]
    snap.add_node(
        GraphNode(
            id="A-worker",
            kind=NodeKind.SYMBOL,
            name="run_job",
            repo_id="A",
            service_id="orders",
            file="worker.py",
        )
    )
    snap.add_edge(
        GraphEdge(
            from_id="A-worker",
            to_id="endpoint::B::ledger::GET:/run",
            kind=EdgeKind.CALLS_API,
            evidence=Evidence(file="worker.py", line=18, rule="base-url:ledger"),
        )
    )
    return snap


@pytest.mark.parametrize(
    "question,intent",
    [
        ("Who calls B::ledger?", "service_inbound"),
        ("What services does A::orders call?", "service_outbound"),
        ("Does A::orders call B::ledger?", "service_to_service"),
    ],
)
def test_service_ask_includes_calls_from_background_symbols(worker_snapshot, question, intent):
    response = ask_deterministic(worker_snapshot, question)

    assert response is not None and response.intent == intent
    assert "0 API(s)" not in response.summary
    assert "A::orders" in response.summary
    assert response.rows[0].source.node_id == "A-worker"
    assert response.rows[0].via is not None and response.rows[0].via.line == 18
    assert response.rows[0].target is not None and response.rows[0].target.repo_id == "B"


def test_api_only_ask_keeps_worker_symbols_out_of_endpoint_count(worker_snapshot):
    response = ask_deterministic(worker_snapshot, "Which APIs call B::ledger?")

    assert response is not None and response.intent == "service_inbound"
    assert "0 API(s)" in response.summary
    assert response.rows == []


def test_worker_caller_affects_its_repo_and_service_without_fabricating_endpoint(worker_snapshot):
    result = compute_impact(worker_snapshot, ["app/main.py"], repo_id="B")

    assert result.affected_repos == ["A", "B"]
    assert result.affected_services == ["A::orders", "B::ledger"]
    assert result.affected_endpoint_ids == ["endpoint::B::ledger::GET:/run"]


def test_impact_unresolved_warnings_are_scoped_before_limit(worker_snapshot):
    snap = worker_snapshot
    for i in range(24):
        node_id = f"external-unrelated-{i}"
        snap.add_node(GraphNode(id=node_id, kind=NodeKind.EXTERNAL_API))
        snap.add_edge(
            GraphEdge(
                from_id="C-handler",
                to_id=node_id,
                kind=EdgeKind.CALLS_API,
                evidence=Evidence(file="app/main.py", line=i + 1, rule="unresolved"),
                props={"url": f"https://unrelated.example/{i}"},
            )
        )
        snap.unresolved.append(f"unresolved outbound call app/main.py:{i + 1} unrelated")
    snap.add_node(GraphNode(id="external-relevant", kind=NodeKind.EXTERNAL_API))
    snap.add_edge(
        GraphEdge(
            from_id="A-worker",
            to_id="external-relevant",
            kind=EdgeKind.CALLS_API,
            evidence=Evidence(file="worker.py", line=21, rule="unresolved"),
            props={"url": "https://relevant.example/run"},
        )
    )
    snap.unresolved.append("unresolved outbound call worker.py:21 https://relevant.example/run")

    result = compute_impact(snap, ["app/main.py"], repo_id="B")

    assert len(result.unresolved) == 1
    assert "worker.py:21" in result.unresolved[0]
    assert "relevant.example" in result.unresolved[0]
