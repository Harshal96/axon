import asyncio

import pytest

from axon.v1.ask import ask, ask_deterministic
from axon.v1.ask_entities import resolve_entities
from axon.v1.models import EdgeKind, GraphEdge, GraphNode, NodeKind
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


def qualified_snapshot():
    from axon.v1.graph import GraphSnapshot
    from axon.v1.models import Evidence, endpoint_node_id, repo_node_id, service_node_id

    snap = GraphSnapshot(publication_id="publication-test")
    for repo, service in [("A", "orders"), ("B", "ledger"), ("C", "ledger")]:
        snap.add_node(
            GraphNode(
                id=repo_node_id(repo),
                kind=NodeKind.REPO,
                repo_id=repo,
                props={"commit": repo + "-commit"},
            )
        )
        snap.add_node(
            GraphNode(
                id=service_node_id(repo, service),
                kind=NodeKind.SERVICE,
                repo_id=repo,
                service_id=service,
                name=service,
            )
        )
        ep = endpoint_node_id(repo, service, "GET", "/run")
        snap.add_node(
            GraphNode(
                id=ep,
                kind=NodeKind.API_ENDPOINT,
                repo_id=repo,
                service_id=service,
                props={"method": "GET", "path": "/run"},
            )
        )
        snap.add_node(
            GraphNode(
                id=repo + "-handler",
                kind=NodeKind.SYMBOL,
                repo_id=repo,
                service_id=service,
                file="app/main.py",
                line_start=1,
                line_end=3,
            )
        )
        snap.add_edge(GraphEdge(from_id=repo + "-handler", to_id=ep, kind=EdgeKind.HANDLES))
    for repo in ("B", "C"):
        snap.add_edge(
            GraphEdge(
                from_id="A-handler",
                to_id=endpoint_node_id(repo, "ledger", "GET", "/run"),
                kind=EdgeKind.CALLS_API,
                evidence=Evidence(rule="client:sdk", file="app/main.py", line=2),
            )
        )
    return snap


def test_qualified_service_alias_and_citations_keep_complete_identity():
    snap = qualified_snapshot()
    response = ask_deterministic(snap, "does A::orders call B::ledger?")
    assert response.intent == "service_to_service"
    assert "A::orders" in response.summary and "B::ledger" in response.summary
    assert len(response.rows) == 1
    assert response.rows[0].source.repo_id == "A"
    assert response.rows[0].target.repo_id == "B"
    assert response.rows[0].target.service_id == "ledger"
    assert response.publication_id == snap.publication_id
    binding = next(b for b in response.bindings if b["repo_id"] == "B")
    assert binding["service_id"] == "ledger"
    assert binding["node_id"] == "service::B::ledger"
    ambiguous = ask_deterministic(snap, "who calls ledger?")
    assert ambiguous.intent == "ambiguous"
    assert "B::ledger" in ambiguous.answer and "C::ledger" in ambiguous.answer


def test_qualified_outbound_counts_do_not_combine_duplicate_services():
    response = ask_deterministic(qualified_snapshot(), "what does A::orders call?")
    assert response.intent == "service_outbound"
    assert "B::ledger (1)" in response.summary
    assert "C::ledger (1)" in response.summary
    inbound = ask_deterministic(qualified_snapshot(), "which APIs call B::ledger?")
    assert len(inbound.rows) == 1 and inbound.rows[0].source.repo_id == "A"


def test_client_gap_target_requires_repo_and_explicit_local_is_local():
    snap = qualified_snapshot()
    snap.edges = [e for e in snap.edges if e.kind != EdgeKind.CALLS_API]
    snap.add_node(
        GraphNode(
            id="client",
            kind=NodeKind.API_CLIENT,
            repo_id="A",
            service_id="orders",
            props={"target_service": "ledger", "target_repo": "B"},
        )
    )
    b = ask_deterministic(snap, "does A::orders call B::ledger?")
    c = ask_deterministic(snap, "does A::orders call C::ledger?")
    assert any("declares a client" in spot for spot in b.blind_spots)
    assert not any("declares a client" in spot for spot in c.blind_spots)


def test_unscoped_coverage_and_resolution_keep_qualified_keys():
    snap = qualified_snapshot()
    assert snap.coverage_by_service()["B::ledger"]["endpoints"] == 1
    assert snap.coverage_by_service()["C::ledger"]["endpoints"] == 1
    assert snap.resolution_report()["B::ledger"]["endpoints"] == 1


def test_qualified_repo_with_slash_and_similar_normalized_alias_stays_exact():
    snap = qualified_snapshot()
    snap.add_node(
        GraphNode(
            id="service::org/B-test::ledger",
            kind=NodeKind.SERVICE,
            repo_id="org/B-test",
            service_id="ledger",
        )
    )
    snap.add_node(
        GraphNode(
            id="service::org/B_test::ledger",
            kind=NodeKind.SERVICE,
            repo_id="org/B_test",
            service_id="ledger",
        )
    )
    bound = resolve_entities(snap, "what does org/B-test::ledger call?")
    assert [node.id for node in bound.services] == ["service::org/B-test::ledger"]
    assert not bound.unresolved and not bound.ambiguous


def test_endpoint_answers_also_expose_legacy_freshness_unknowns():
    response = ask_deterministic(qualified_snapshot(), "can GET /run in B::ledger be deprecated?")
    assert response.intent == "deprecatable"
    assert any("link-inputs-missing" in spot for spot in response.blind_spots)


def test_mixed_qualified_and_bare_service_preserves_question_direction():
    response = ask_deterministic(qualified_snapshot(), "does orders call B::ledger?")
    assert response.intent == "service_to_service"
    assert len(response.rows) == 1 and response.rows[0].source.repo_id == "A"


@pytest.mark.parametrize("engine", ["auto", "deterministic", "model"])
@pytest.mark.parametrize(
    "question,intent",
    [
        ("who calls ledger?", "ambiguous"),
        ("who calls absent::ledger?", "unknown"),
        ("list APIs in missing-service", "unknown"),
        ("who calls from service missing-service?", "unknown"),
        ("who calls service missing-service?", "unknown"),
        ("list APIs for service missing-service", "unknown"),
        ("who calls service 'missing-service'?", "unknown"),
        ("who calls 'ledger'?", "ambiguous"),
    ],
)
def test_all_engines_require_unambiguous_existing_ownership(question, intent, engine):
    drafts, executions = [], []

    class Model:
        async def draft_cypher(self, prompt):
            drafts.append(prompt)
            return "MATCH (n:ApiEndpoint) RETURN n.id AS id"

    class Store:
        def run_cypher(self, query):
            executions.append(query)
            return [{"id": "endpoint::B::ledger::GET:/run"}]

    response = asyncio.run(
        ask(qualified_snapshot(), question, llm=Model(), store=Store(), engine=engine)
    )
    assert response.intent == intent and not response.grounded
    assert not response.model_used and not drafts and not executions
    assert response.publication_id == "publication-test"


@pytest.mark.parametrize(
    "value", ["pending-review", "ledger", "absent::ledger", "service missing-service"]
)
def test_model_qualified_scope_allows_unrelated_quoted_filter_value(value):
    question = f"Find endpoint rows in B::ledger where status equals '{value}'"
    binding = resolve_entities(qualified_snapshot(), question)
    # The default deterministic resolver still treats ordinary quoted entities
    # as graph evidence; Model ownership resolution must treat filter data apart.
    if value == "ledger":
        assert binding.ambiguous
    elif value == "pending-review":
        assert value in binding.unresolved
    drafts = []

    class Model:
        async def draft_cypher(self, prompt):
            drafts.append(prompt)
            return "MATCH (n:ApiEndpoint) RETURN n.id AS id"

    class Store:
        def run_cypher(self, query):
            return [{"id": "endpoint::B::ledger::GET:/run"}]

    response = asyncio.run(
        ask(qualified_snapshot(), question, llm=Model(), store=Store(), engine="model")
    )
    assert len(drafts) == 1
    assert response.model_used and response.intent == "cypher"
    assert response.evidence == [{"id": "endpoint::B::ledger::GET:/run"}]


@pytest.mark.parametrize(
    "question",
    [
        "Which service exposes GET /orders?",
        "What service provides endpoints?",
        "List all service IDs",
    ],
)
def test_model_generic_service_subject_does_not_name_a_missing_service(question):
    prompts = []

    class Model:
        async def draft_cypher(self, prompt):
            prompts.append(prompt)
            return "MATCH (n:Service) RETURN n.id AS id"

    class Store:
        def run_cypher(self, query):
            return [{"id": "service::A::orders"}]

    response = asyncio.run(
        ask(qualified_snapshot(), question, llm=Model(), store=Store(), engine="model")
    )
    assert response.model_used and response.intent == "cypher"
    assert len(prompts) == 1
    assert response.evidence == [{"id": "service::A::orders"}]


@pytest.mark.parametrize(
    "comparison",
    [
        "service equals 'pending-review'",
        "service is 'pending-review'",
        "service contains 'ledger'",
        "service matches 'ledger'",
        "service = 'ledger'",
        'service != "ledger"',
        "ledger equals 'pending-review'",
        "n.service equals 'ledger'",
    ],
)
def test_model_comparison_fields_and_operators_are_data(comparison):
    question = f"Find endpoint rows in B::ledger where {comparison}"
    prompts = []

    class Model:
        async def draft_cypher(self, prompt):
            prompts.append(prompt)
            return "MATCH (n:ApiEndpoint) RETURN n.id AS id"

    class Store:
        def run_cypher(self, query):
            return [{"id": "endpoint::B::ledger::GET:/run"}]

    response = asyncio.run(
        ask(qualified_snapshot(), question, llm=Model(), store=Store(), engine="model")
    )
    assert response.model_used and response.intent == "cypher"
    assert len(prompts) == 1 and question in prompts[0]
    binding = resolve_entities(qualified_snapshot(), question, filter_values_as_data=True)
    assert [row["node_id"] for row in binding.records] == ["service::B::ledger"]
    assert "service::B::ledger" in prompts[0]
    assert response.evidence == [{"id": "endpoint::B::ledger::GET:/run"}]


def test_model_comparison_before_owner_keeps_qualified_binding():
    question = "Find endpoint rows where service equals 'pending-review' in B::ledger"
    prompts = []

    class Model:
        async def draft_cypher(self, prompt):
            prompts.append(prompt)
            return "MATCH (n:ApiEndpoint) RETURN n.id AS id"

    class Store:
        def run_cypher(self, query):
            return [{"id": "endpoint::B::ledger::GET:/run"}]

    response = asyncio.run(
        ask(qualified_snapshot(), question, llm=Model(), store=Store(), engine="model")
    )
    assert response.model_used and len(prompts) == 1 and question in prompts[0]
    binding = resolve_entities(qualified_snapshot(), question, filter_values_as_data=True)
    assert binding.records[0]["span"] == "B::ledger"
    assert binding.records[0]["node_id"] == "service::B::ledger"
    assert "service::B::ledger" in prompts[0]
