import pytest

from axon.v2.ask import ask_deterministic
from axon.v2.impact import compute_impact, render_markdown
from axon.v2.llm import validate_readonly_cypher
from axon.v2.onboard import scan_repo
from axon.v2.servicemap import ServiceMap, dump_service_map, load_service_map

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

    from axon.v2.ask import ask

    hit = asyncio.run(
        ask(monorepo_snap, "Can GET /v1/legacy be deprecated?", engine="deterministic")
    )
    assert hit.intent == "deprecatable" and not hit.model_used
    miss = asyncio.run(ask(monorepo_snap, "Why is the sky blue?", engine="deterministic"))
    assert miss.intent == "unknown" and "Deterministic" in miss.answer


def test_ask_engine_model_skips_deterministic(monorepo_snap):
    import asyncio

    from axon.v2.ask import ask

    # No model configured: the deterministic answer that Auto would
    # give must NOT appear when the Model engine is forced.
    resp = asyncio.run(ask(monorepo_snap, "Can GET /v1/legacy be deprecated?", engine="model"))
    assert resp.intent == "unknown" and "Model engine" in resp.answer
