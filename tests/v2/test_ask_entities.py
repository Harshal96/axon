import asyncio

from axon.v2.ask import ask, ask_deterministic
from axon.v2.ask_entities import resolve_entities
from axon.v2.models import EdgeKind, GraphEdge, GraphNode, NodeKind
from tests.evals.ask.fixture import snapshot


def test_unknown_target_never_answers_a_different_service_question():
    response = asyncio.run(ask(snapshot(), "does scheduler call missing-service?", engine="auto"))
    assert response.intent == "unknown"
    assert not response.grounded


def test_explicit_missing_path_does_not_answer_for_whole_service():
    response = asyncio.run(
        ask(snapshot(), "what services does scheduler GET /missing call", engine="auto")
    )
    assert response.intent == "unknown"


def test_reverse_and_transitive_rows_keep_proof():
    snap = snapshot()
    target = next(
        n
        for n in snap.nodes.values()
        if n.kind == NodeKind.API_ENDPOINT and n.service_id == "ledger"
    )
    snap.add_edge(GraphEdge(from_id="entry", to_id=target.id, kind=EdgeKind.CALLS_API))
    direct = ask_deterministic(snap, "does scheduler call ledger?")
    indirect = ask_deterministic(snap, "does scheduler indirectly call ledger?")
    assert direct is not None and not direct.rows
    assert indirect is not None and len(indirect.rows) == 1
    assert indirect.rows[0].target.node_id == target.id
    assert indirect.rows[0].via.node_id == "entry"


def test_same_path_requires_method_and_same_alias_requires_repo():
    snap = snapshot()
    duplicate = GraphNode(
        id="duplicate", kind=NodeKind.API_ENDPOINT, props={"method": "POST", "path": "/runs/{id}"}
    )
    snap.add_node(duplicate)
    assert resolve_entities(snap, "who calls /runs/{id}").ambiguous
    bound = resolve_entities(snap, "who calls GET /runs/{id}")
    assert len(bound.endpoints) == 1 and not bound.ambiguous
    snap.add_node(
        GraphNode(
            id="other-service",
            kind=NodeKind.SERVICE,
            repo_id="other",
            service_id="scheduler",
            name="Scheduler",
        )
    )
    assert ask_deterministic(snap, "does scheduler call directory").intent == "ambiguous"


def test_index_invalidates_on_nodes_and_handles():
    snap = snapshot()
    assert not snap.endpoint_of_symbol("Directory")
    target = next(n for n in snap.nodes.values() if n.kind == NodeKind.API_ENDPOINT)
    snap.add_edge(GraphEdge(from_id="Directory", to_id=target.id, kind=EdgeKind.HANDLES))
    assert [n.id for n in snap.endpoint_of_symbol("Directory")] == [target.id]
    snap.add_node(GraphNode(id="new", kind=NodeKind.SYMBOL, name="new_symbol"))
    assert resolve_entities(snap, "function new_symbol").symbols[0].id == "new"


def test_dictionary_word_is_not_a_symbol_but_explicit_symbol_is():
    snap = snapshot()
    assert not resolve_entities(snap, "which apis use service").symbols
    assert resolve_entities(snap, "which apis include the class Service").symbols


def test_unresolved_catalog_qualifiers_never_become_unfiltered_listings():
    for question in ["list APIs in missing-service", "list APIs in scheduler at /missing"]:
        response = ask_deterministic(snapshot(), question)
        assert response is not None and response.intent == "unknown"
        assert not response.grounded


def test_operations_and_symbols_use_explicit_service_scope():
    snap = snapshot()
    for service in ["scheduler", "directory"]:
        snap.add_node(
            GraphNode(
                id=f"operation-{service}",
                kind=NodeKind.API_ENDPOINT,
                repo_id="sample",
                service_id=service,
                props={"operation_id": "listThings", "method": "GET", "path": "/things"},
            )
        )
        snap.add_node(
            GraphNode(
                id=f"symbol-{service}",
                kind=NodeKind.SYMBOL,
                repo_id="sample",
                service_id=service,
                name="build_things",
            )
        )
    bound = resolve_entities(snap, "who calls listThings in scheduler service")
    assert [n.id for n in bound.endpoints] == ["operation-scheduler"]
    assert not bound.ambiguous
    symbols = resolve_entities(snap, "which APIs in scheduler include function build_things")
    assert [n.id for n in symbols.symbols] == ["symbol-scheduler"]


def test_multi_symbol_summary_keeps_zero_result_findings():
    snap = snapshot()
    snap.add_node(GraphNode(id="unused", kind=NodeKind.SYMBOL, name="unused_helper"))
    response = ask_deterministic(snap, "which apis include `price_cart` and `unused_helper`")
    assert response.rows
    assert "unused_helper" in response.summary
    assert "0 API" in response.summary


def test_paths_and_qualified_symbols_do_not_consume_service_words():
    snap = snapshot()
    snap.add_node(
        GraphNode(
            id="nested-path",
            kind=NodeKind.API_ENDPOINT,
            repo_id="sample",
            service_id="scheduler",
            props={"method": "GET", "path": "/directory/events"},
        )
    )
    snap.add_node(
        GraphNode(
            id="qualified",
            kind=NodeKind.SYMBOL,
            name="from_entry",
            repo_id="sample",
            props={"qualified": "Directory.from_entry"},
        )
    )
    path = resolve_entities(snap, "who calls GET /directory/events")
    assert not path.services
    assert [n.id for n in path.endpoints] == ["nested-path"]
    code = resolve_entities(snap, "which apis include `Directory.from_entry`")
    assert not code.services
    assert [n.id for n in code.symbols] == ["qualified"]


def test_callers_of_operation_does_not_treat_operation_as_missing_service():
    snap = snapshot()
    snap.add_node(
        GraphNode(
            id="named",
            kind=NodeKind.API_ENDPOINT,
            repo_id="sample",
            service_id="directory",
            props={"method": "GET", "path": "/named", "operation_id": "readThings"},
        )
    )
    response = ask_deterministic(snap, "callers of readThings")
    assert response is not None and response.intent == "callers"


def test_source_position_can_follow_target_and_bindings_expose_roles():
    response = ask_deterministic(snapshot(), "which APIs call directory from scheduler")
    assert response is not None and response.intent == "service_to_service"
    assert len(response.rows) == 2
    roles = {b["id"]: b.get("role") for b in response.bindings}
    assert roles == {"directory": "target", "scheduler": "source"}
