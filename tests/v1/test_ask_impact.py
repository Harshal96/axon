import pytest

from axon.v1.ask import ask_deterministic
from axon.v1.impact import compute_impact, render_markdown
from axon.v1.llm import validate_readonly_cypher
from axon.v1.onboard import scan_repo
from axon.v1.servicemap import ServiceMap, dump_service_map, load_service_map

pytestmark = pytest.mark.tier1


def test_ask_what_services_does_api_call(monorepo_snap):
    resp = ask_deterministic(monorepo_snap, "What services does POST /checkout call?")
    assert resp is not None
    assert resp.intent == "outbound"
    assert "payment" in resp.answer
    assert resp.citations


def test_ask_function_apis(monorepo_snap):
    resp = ask_deterministic(monorepo_snap, "Which APIs is the function price_cart a part of?")
    assert resp is not None
    assert "/checkout" in resp.answer


def test_ask_deprecatable_with_callers(monorepo_snap):
    resp = ask_deterministic(monorepo_snap, "Can POST /v1/charges be deprecated?")
    assert resp is not None
    assert resp.answer.startswith("No")


def test_ask_deprecatable_without_callers_is_hedged(monorepo_snap):
    resp = ask_deterministic(monorepo_snap, "Can GET /v1/legacy be deprecated?")
    assert resp is not None
    assert "candidate" in resp.answer
    assert "not visible" in resp.answer  # never claims absolute safety


def test_ask_unknown_without_model(monorepo_snap):
    assert ask_deterministic(monorepo_snap, "Why is the sky blue?") is None


def test_impact_shared_function_reaches_callers(monorepo_snap):
    result = compute_impact(monorepo_snap, ["shared/util.py"], repo_id="fixture-monorepo")
    joined = " ".join(result.affected_apis)
    assert "/v1/charges" in joined
    assert "/checkout" in joined  # reverse reachability through CALLS_API
    assert "fixture-monorepo::payment" in result.affected_services
    assert "Axon API impact" in render_markdown(result)


def test_impact_spec_change(monorepo_snap):
    result = compute_impact(
        monorepo_snap, ["services/payment/openapi.yaml"], repo_id="fixture-monorepo"
    )
    assert any("/checkout" in a for a in result.affected_apis)


def test_cypher_guard_rejects_writes():
    assert validate_readonly_cypher("MATCH (n) RETURN n")[0]
    assert not validate_readonly_cypher("MATCH (n) DELETE n RETURN n")[0]
    assert not validate_readonly_cypher("CREATE (n) RETURN n")[0]
    assert not validate_readonly_cypher("MATCH (n) RETURN n; DROP INDEX x")[0]
    assert not validate_readonly_cypher("MATCH (n) SET n.x = 1 RETURN n")[0]


def test_service_map_roundtrip_and_validation(fixtures):
    sm = load_service_map(str(fixtures / "monorepo"))
    assert sm.repo == "fixture-monorepo"
    text = dump_service_map(sm)
    assert ServiceMap.model_validate(__import__("yaml").safe_load(text))
    with pytest.raises(Exception):
        ServiceMap.model_validate({"services": [{"id": "a"}, {"id": "a"}]})


def test_onboard_scan_drafts_services(fixtures, tmp_path):
    report = scan_repo(str(fixtures / "monorepo"))
    assert "services:" in report.draft_yaml
    assert report.services


def test_ask_engine_deterministic_never_needs_model(monorepo_snap):
    import asyncio

    from axon.v1.ask import ask

    hit = asyncio.run(
        ask(monorepo_snap, "Can GET /v1/legacy be deprecated?", engine="deterministic")
    )
    assert hit.intent == "deprecatable" and not hit.model_used
    miss = asyncio.run(ask(monorepo_snap, "Why is the sky blue?", engine="deterministic"))
    assert miss.intent == "unknown" and "Deterministic" in miss.answer


def test_ask_engine_model_skips_deterministic(monorepo_snap):
    import asyncio

    from axon.v1.ask import ask

    # No model configured: the deterministic answer that Auto would
    # give must NOT appear when the Model engine is forced.
    resp = asyncio.run(ask(monorepo_snap, "Can GET /v1/legacy be deprecated?", engine="model"))
    assert resp.intent == "unknown" and "Model engine" in resp.answer


def test_graph_impact_seeds_only_selected_repo_and_never_publishes():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axon.v1.api import router
    from axon.v1.store import MemoryStore
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    store = MemoryStore(snap)
    app = FastAPI()
    app.state.v1_store = store
    app.include_router(router, prefix="/api/v1")
    before = store.load().model_dump_json()
    client = TestClient(app)
    response = client.post(
        "/api/v1/impact/graph", json={"repo_id": "B", "changed_files": ["app/main.py"]}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["affected_repos"] == ["A", "B"]
    assert result["changed_symbols"] == ["B-handler"]
    assert len(result["affected_endpoint_ids"]) == 2
    assert result["publication_id"] == snap.publication_id
    assert result["indexed_commit"] == "B-commit"
    assert any("delet" in limit.lower() for limit in result["limitations"])
    assert any("baseline" in limit.lower() for limit in result["limitations"])
    assert store.load().model_dump_json() == before
    assert (
        client.post(
            "/api/v1/impact/graph", json={"repo_id": "missing", "changed_files": []}
        ).status_code
        == 404
    )
    stale = client.post(
        "/api/v1/impact/graph", json={"repo_id": "B", "changed_files": [], "expected_commit": "new"}
    )
    assert stale.status_code == 409 and "B-commit" in stale.text


def test_model_execution_errors_are_safe_for_repair_and_public_answer():
    import asyncio

    from axon.v1.ask import ask
    from axon.v1.graph import GraphSnapshot

    sentinel = "private-physical-generation-sentinel"
    prompts = []

    class Model:
        async def draft_cypher(self, prompt):
            prompts.append(prompt)
            return "MATCH (n:Repo) RETURN n.id AS id"

    class Store:
        def run_cypher(self, query):
            raise RuntimeError("_axon_storage_id=" + sentinel)

    result = asyncio.run(
        ask(GraphSnapshot(), "find repos", llm=Model(), store=Store(), engine="model")
    )
    assert result.intent == "cypher_error"
    assert len(prompts) == 3
    assert sentinel not in str(prompts) and "_axon_storage_id" not in str(prompts)
    assert sentinel not in result.answer and "_axon_storage_id" not in result.answer


def test_graph_impact_does_not_seed_legacy_blank_ownership():
    from axon.v1.models import EdgeKind, GraphEdge, GraphNode, NodeKind
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    snap.add_node(GraphNode(id="unowned", kind=NodeKind.SYMBOL, file="unowned.py"))
    snap.add_edge(GraphEdge(from_id="unowned", to_id="C-handler", kind=EdgeKind.CALLS))
    result = compute_impact(snap, ["unowned.py"], repo_id="B")
    assert not result.changed_symbols and not result.affected_endpoint_ids


def test_model_query_is_not_stamped_with_old_drafting_snapshot():
    import asyncio

    from axon.v1.ask import ask
    from axon.v1.graph import GraphSnapshot

    snap = GraphSnapshot(publication_id="before-draft")

    class Model:
        async def draft_cypher(self, prompt):
            return "MATCH (n:Repo) RETURN n.id AS id"

    class Store:
        def run_cypher(self, query):
            # A publication committed while drafting: these rows are newer.
            return [{"id": "new-repository"}]

    response = asyncio.run(ask(snap, "list repos", llm=Model(), store=Store(), engine="model"))
    assert response.evidence == [{"id": "new-repository"}]
    assert response.publication_id == ""
    assert any("publication" in spot and "not captured" in spot for spot in response.blind_spots)


@pytest.mark.parametrize("bootstrapped", [False, True])
def test_impact_separates_known_historical_traversals_from_unknown_caller_coverage(bootstrapped):
    from axon.v1.models import EdgeKind
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    if bootstrapped:
        for edge in snap.edges:
            if edge.kind == EdgeKind.CALLS_API:
                edge.props["relink_status"] = "pinned-legacy"
    result = compute_impact(snap, ["app/main.py"], repo_id="B")
    assert result.affected_repos == ["A", "B"]
    known = [limit for limit in result.limitations if "Known" in limit and "pinned-legacy" in limit]
    assert len(known) == 1 and "A" in known[0] and "fresh matching" in known[0]
    assert all("C" not in limit for limit in known)
    for repo in ("A", "B", "C"):
        assert any(
            f"{repo}:" in limit and "retained link inputs" in limit and "Re-index" in limit
            for limit in result.limitations
        )
    assert any("possibly omitted callers" in limit for limit in result.limitations)
    assert "pinned-legacy" in render_markdown(result)


def complete_impact_inputs(snap):
    from axon.v1.link_inputs import RepoLinkInputs, RepoLinkReport

    for repo, service in [("A", "orders"), ("B", "ledger"), ("C", "ledger")]:
        snap.link_inputs[repo] = RepoLinkInputs(
            repo_id=repo,
            source_generation=f"generation-{repo}",
            service_map=ServiceMap.model_validate({"repo": repo, "services": [{"id": service}]}),
        )
        snap.link_reports[repo] = RepoLinkReport()


def test_complete_impact_inputs_do_not_add_legacy_warnings():
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    complete_impact_inputs(snap)
    result = compute_impact(snap, ["app/main.py"], repo_id="B")
    assert result.affected_repos == ["A", "B"]
    assert not any(
        "retained link inputs" in limit or "pinned-legacy" in limit for limit in result.limitations
    )


def test_missing_inputs_warn_about_possible_callers_without_guessing_participation():
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    complete_impact_inputs(snap)
    del snap.link_inputs["C"]
    result = compute_impact(snap, ["app/main.py"], repo_id="B")
    assert result.affected_repos == ["A", "B"]
    assert not any("Known" in limit for limit in result.limitations)
    warnings = [limit for limit in result.limitations if "retained link inputs" in limit]
    assert len(warnings) == 1 and "C:" in warnings[0] and "possibly omitted callers" in warnings[0]


def test_retained_pins_warn_even_when_inputs_are_present():
    from axon.v1.models import EdgeKind
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    complete_impact_inputs(snap)
    for edge in snap.edges:
        if edge.kind == EdgeKind.CALLS_API:
            edge.props["relink_status"] = "pinned-legacy"
    result = compute_impact(snap, ["app/main.py"], repo_id="B")
    assert any("Known" in limit and "A" in limit for limit in result.limitations)
    assert any("A:" in limit and "caller coverage" in limit for limit in result.limitations)
    assert not any("retained link inputs" in limit for limit in result.limitations)
