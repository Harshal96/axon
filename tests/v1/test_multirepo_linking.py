"""Hand-authored contracts for caller-owned replay against a shared catalog."""

from copy import deepcopy

import pytest

from axon.v1.endpoint_catalog import EndpointCatalog
from axon.v1.facts import CallFact, FileFacts, HttpFact
from axon.v1.graph import GraphSnapshot
from axon.v1.link_inputs import RepoLinkInputs
from axon.v1.models import (
    Confidence,
    EdgeKind,
    Evidence,
    GraphEdge,
    GraphNode,
    NodeKind,
    endpoint_node_id,
    external_node_id,
    repo_node_id,
    service_node_id,
    symbol_node_id,
)
from axon.v1.servicemap import ServiceMap


def repository(repo, service, *, endpoint=True, host=False):
    sm = ServiceMap.model_validate(
        {
            "repo": repo,
            "services": [
                {
                    "id": service,
                    "roots": [service],
                    "base_urls": ["https://ledger.example"] if host else [],
                }
            ],
        }
    )
    snap = GraphSnapshot()
    for node in [
        GraphNode(id=repo_node_id(repo), kind=NodeKind.REPO, repo_id=repo),
        GraphNode(
            id=service_node_id(repo, service),
            kind=NodeKind.SERVICE,
            repo_id=repo,
            service_id=service,
        ),
        GraphNode(
            id=symbol_node_id(repo, service + "/api.py", "handle"),
            kind=NodeKind.SYMBOL,
            repo_id=repo,
            service_id=service,
            file=service + "/api.py",
        ),
    ]:
        snap.add_node(node)
    if endpoint:
        path = "/orders" if service == "orders" else "/entries"
        eid = endpoint_node_id(repo, service, "GET", path)
        snap.add_node(
            GraphNode(
                id=eid,
                kind=NodeKind.API_ENDPOINT,
                repo_id=repo,
                service_id=service,
                props={
                    "method": "GET",
                    "path": path,
                    "operation_id": "listOrders" if service == "orders" else "getEntries",
                },
            )
        )
        snap.add_edge(
            GraphEdge(
                from_id=symbol_node_id(repo, service + "/api.py", "handle"),
                to_id=eid,
                kind=EdgeKind.HANDLES,
            )
        )
    snap.link_inputs[repo] = RepoLinkInputs(repo, sm)
    return snap


def caller(*, accessor=False, explicit_base=True, repo="A", target_repo="B"):
    from axon.v1.repository_links import prepare_link_inputs

    snap = repository(repo, "orders")
    sm = ServiceMap.model_validate(
        {
            "repo": repo,
            "services": [
                {
                    "id": "orders",
                    "roots": ["orders"],
                    "clients": [
                        {
                            "id": "ledger-client",
                            "packages": ["ledger_sdk"],
                            "target_repo": target_repo,
                            "target_service": "ledger",
                            "base_urls": ["https://ledger.example"] if explicit_base else [],
                        }
                    ],
                    "calls": [
                        {
                            "id": "audit",
                            "from_api": "GET /orders",
                            "to": {
                                "repo": target_repo,
                                "service": "ledger",
                                "method": "GET",
                                "path": "/entries",
                            },
                            "evidence": {"file": "orders/api.py", "line": 30, "text": "audit()"},
                        }
                    ],
                }
            ],
        }
    )
    facts = FileFacts(
        "orders/api.py",
        "python",
        http_calls=[
            HttpFact(
                "handle",
                10,
                via="generated-client",
                call_name="ledger_sdk.getEntries",
                client_id="ledger-client",
                client_owner="orders",
                binding_rule="factory:inferred-return",
                detail="orders/factory.py:4 return ledger_sdk",
            ),
            HttpFact("handle", 20, "GET", "https://ledger.example/entries", "requests"),
            HttpFact("handle", 40, via="sdk", call_name="s3.put_object", provider="aws"),
        ],
        calls=[
            CallFact("handle", "ledger_sdk.getEntries", 10, argument_count=0 if accessor else 1)
        ],
    )
    snap.link_inputs[repo] = prepare_link_inputs(
        snap, sm, [facts], {"call:orders/audit": "overrides.yaml"}
    )
    return snap


def publish(base, incoming, repo):
    from axon.v1.repository_links import reconcile_repositories

    return reconcile_repositories(base, incoming, replace_repo_ids={repo})


def outbound(snap):
    return {e.evidence.line: e for e in snap.edges if e.kind == EdgeKind.CALLS_API}


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
def test_index_orders_have_exact_cross_repo_links_and_reports(order):
    repos = {"A": caller(), "B": repository("B", "ledger", host=True)}
    snap = GraphSnapshot()
    for repo in order:
        snap = publish(snap, repos[repo], repo)
    target = "endpoint::B::ledger::GET:/entries"
    edges = outbound(snap)
    assert {edges[n].to_id for n in (10, 20, 30)} == {target}
    assert edges[10].confidence == edges[20].confidence == Confidence.INFERRED
    assert edges[10].evidence.rule == "client:ledger-client"
    assert edges[10].props["binding_rule"] == "factory:inferred-return"
    assert edges[10].evidence.detail == "orders/factory.py:4 return ledger_sdk"
    assert edges[20].evidence.rule == "base-url:ledger.example"
    assert edges[30].confidence == Confidence.MANUAL
    assert edges[30].evidence == Evidence(
        file="orders/api.py", line=30, rule="manual:overrides.yaml#audit", detail="audit()"
    )
    assert snap.link_reports["A"].coverage["orders"] == {
        "endpoints": 1,
        "with_handler": 1,
        "outbound_resolved": 3,
        "outbound_external": 0,
        "external_dependencies": 1,
    }
    assert snap.link_reports["A"].unresolved == []
    assert set(snap.link_reports) == {"A", "B"}


def test_missing_explicit_target_never_resolves_to_foreign_namesake():
    snap = publish(GraphSnapshot(), caller(), "A")
    snap = publish(snap, repository("C", "ledger", host=True), "C")
    assert all(snap.nodes[e.to_id].kind == NodeKind.EXTERNAL_API for e in outbound(snap).values())
    assert outbound(snap)[10].to_id == external_node_id("ANY", "ledger_sdk.getEntries")
    # A diagnostic candidate is not permission to broaden an explicit binding.
    assert outbound(snap)[10].props["candidates"] == '["endpoint::C::ledger::GET:/entries"]'
    assert outbound(snap)[20].props["candidates"] == '["endpoint::C::ledger::GET:/entries"]'
    assert len(snap.link_reports["A"].unresolved) == 3
    assert outbound(snap)[10].props["binding_rule"] == "factory:inferred-return"
    assert outbound(snap)[10].evidence.detail == "orders/factory.py:4 return ledger_sdk"
    arrived = publish(snap, repository("B", "ledger", host=True), "B")
    assert outbound(arrived)[10].to_id == "endpoint::B::ledger::GET:/entries"
    assert outbound(arrived)[20].to_id == "endpoint::B::ledger::GET:/entries"


@pytest.mark.parametrize("remove", ["route", "service", "repo"])
def test_target_removal_replays_callers_without_mutating_prior_snapshot(remove):
    from axon.v1.repository_links import reconcile_repositories

    before = publish(
        publish(GraphSnapshot(), caller(), "A"), repository("B", "ledger", host=True), "B"
    )
    serialized = before.model_dump(mode="json")
    before.call_closure(symbol_node_id("A", "orders/api.py", "handle"))
    replacement = repository("B", "ledger", endpoint=False)
    if remove == "service":
        replacement.nodes = {
            key: node for key, node in replacement.nodes.items() if node.kind == NodeKind.REPO
        }
        replacement.link_inputs["B"].service_map.services = []
    after = reconcile_repositories(
        before,
        GraphSnapshot() if remove == "repo" else replacement,
        replace_repo_ids=set() if remove == "repo" else {"B"},
        remove_repo_ids=frozenset({"B"}) if remove == "repo" else frozenset(),
    )
    assert all(
        after.nodes[outbound(after)[line].to_id].kind == NodeKind.EXTERNAL_API
        for line in (10, 20, 30)
    )
    assert before.model_dump(mode="json") == serialized
    assert len(after.link_reports["A"].unresolved) == 3


def test_accessor_is_retained_before_suppression_and_becomes_operation():
    snap = publish(GraphSnapshot(), caller(accessor=True), "A")
    assert 10 not in outbound(snap)
    assert len(snap.link_inputs["A"].prepared) == 3
    assert any("model accessor: 1" in d for d in snap.link_reports["A"].diagnostics)
    arrived = publish(snap, repository("B", "ledger"), "B")
    assert outbound(arrived)[10].to_id == "endpoint::B::ledger::GET:/entries"
    assert not any("model accessor" in d for d in arrived.link_reports["A"].diagnostics)


def test_host_collision_remains_external_and_replay_is_idempotent():
    from axon.v1.repository_links import reconcile_repositories

    snap = publish(GraphSnapshot(), caller(explicit_base=False), "A")
    for repo in ("B", "C"):
        snap = publish(snap, repository(repo, "ledger", host=True), repo)
    edge = outbound(snap)[20]
    assert snap.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API
    assert edge.evidence.rule == "unresolved:path-ambiguous"
    assert (
        edge.props["candidates"]
        == '["endpoint::B::ledger::GET:/entries", "endpoint::C::ledger::GET:/entries"]'
    )
    replayed = reconcile_repositories(snap, GraphSnapshot(), replace_repo_ids=set())
    assert replayed.model_dump(mode="json") == snap.model_dump(mode="json")
    replacement = caller(explicit_base=False)
    replacement.link_inputs["A"].prepared = [
        i for i in replacement.link_inputs["A"].prepared if i.fact.line != 20
    ]
    updated = publish(snap, replacement, "A")
    assert 20 not in outbound(updated)
    assert updated.link_reports["B"] == snap.link_reports["B"]
    assert not updated.link_reports["A"].unresolved


def test_resolve_is_pure_and_does_not_copy_foreign_targets_into_structure():
    from axon.v1.repository_links import resolve_repo_links

    a, b = caller(), repository("B", "ledger", host=True)
    catalog = EndpointCatalog.from_snapshot(b)
    original = a.model_dump(mode="json")
    result = resolve_repo_links(a, a.link_inputs["A"], catalog)
    assert {e.to_id for e in result.edges if e.evidence.line != 40} == {
        "endpoint::B::ledger::GET:/entries"
    }
    assert a.model_dump(mode="json") == original
    assert set(result.placeholders) == {external_node_id("SDK", "aws:put_object")}
    assert all(n.repo_id != "B" for n in a.nodes.values())


def test_legacy_links_pin_exact_target_and_preserve_evidence_after_removal():
    from axon.v1.repository_links import reconcile_repositories

    old = repository("A", "orders")
    old.link_inputs.clear()
    target = repository("B", "ledger")
    old.nodes.update(target.nodes)
    old.link_inputs.update(target.link_inputs)
    evidence = Evidence(file="orders/api.py", line=12, rule="old:rule", detail="kept")
    old.add_edge(
        GraphEdge(
            from_id=symbol_node_id("A", "orders/api.py", "handle"),
            to_id="endpoint::B::ledger::GET:/entries",
            kind=EdgeKind.CALLS_API,
            confidence=Confidence.INFERRED,
            evidence=evidence,
            props={"bound": "{}"},
        )
    )
    replayed = reconcile_repositories(old, GraphSnapshot(), replace_repo_ids=set())
    assert outbound(replayed)[12].evidence == evidence
    assert outbound(replayed)[12].props["relink_status"] == "pinned-legacy"
    assert any("link-inputs-missing" in d for d in replayed.link_reports["A"].diagnostics)
    removed = reconcile_repositories(
        replayed, GraphSnapshot(), replace_repo_ids=set(), remove_repo_ids=frozenset({"B"})
    )
    edge = outbound(removed)[12]
    assert edge.evidence == evidence
    assert edge.confidence == Confidence.INFERRED
    assert edge.props["relink_status"] == "pinned-legacy"
    assert removed.nodes[edge.to_id].props["target_endpoint"] == "endpoint::B::ledger::GET:/entries"


@pytest.mark.parametrize("sdk", [False, True])
def test_legacy_external_pins_preserve_target_classification_and_reports(sdk):
    from axon.v1.repository_links import reconcile_repositories

    old = repository("A", "orders")
    old.link_inputs.clear()
    target = GraphNode(
        id=external_node_id("SDK", "aws:put_object")
        if sdk
        else external_node_id("GET", "https://outside.example/items"),
        kind=NodeKind.EXTERNAL_API,
        name="put_object" if sdk else "GET https://outside.example/items",
        props={"kind": "sdk", "provider": "aws", "operation": "s3.put_object"}
        if sdk
        else {"method": "GET", "url": "https://outside.example/items", "candidates": "[]"},
    )
    old.add_node(target)
    edge = GraphEdge(
        from_id=symbol_node_id("A", "orders/api.py", "handle"),
        to_id=target.id,
        kind=EdgeKind.CALLS_API,
        confidence=Confidence.EXTRACTED if sdk else Confidence.AMBIGUOUS,
        evidence=Evidence(
            file="orders/api.py", line=12, rule="external:sdk" if sdk else "unresolved:requests"
        ),
        props={"kind": "sdk", "provider": "aws"}
        if sdk
        else {"method": "GET", "url": "https://outside.example/items"},
    )
    old.add_edge(edge)
    old.unresolved = (
        [] if sdk else ["unresolved outbound call orders/api.py:12  https://outside.example/items"]
    )
    original = old.model_dump(mode="json")
    coverage = old.coverage_by_service("A")
    replayed = reconcile_repositories(old, GraphSnapshot(), replace_repo_ids=set())
    result = outbound(replayed)[12]
    assert result.to_id == target.id
    assert replayed.nodes[target.id] == target
    assert result.evidence == edge.evidence
    assert result.confidence == edge.confidence
    assert result.props["relink_status"] == "pinned-legacy"
    assert replayed.link_reports["A"].coverage == coverage
    assert replayed.link_reports["A"].unresolved == old.unresolved
    assert replayed.unresolved == old.unresolved
    assert not any("pinned link target not indexed" in line for line in replayed.unresolved)
    if sdk:
        assert coverage["orders"]["external_dependencies"] == 1
        assert coverage["orders"]["outbound_external"] == 0
    assert old.model_dump(mode="json") == original
    again = reconcile_repositories(replayed, GraphSnapshot(), replace_repo_ids=set())
    assert again.model_dump(mode="json") == replayed.model_dump(mode="json")


def test_pinned_edges_are_excluded_from_fresh_path_consensus():
    from axon.v1.link_inputs import PinnedCallInput
    from axon.v1.repository_links import prepare_link_inputs, resolve_repo_links

    snap = repository("A", "orders")
    fact = HttpFact("handle", 10, "GET", "{env:REMOTE_URL}/orders", "requests")
    inputs = prepare_link_inputs(
        snap,
        ServiceMap(),
        [FileFacts("orders/api.py", "python", http_calls=[fact])],
        {},
    )
    pinned = GraphEdge(
        from_id=symbol_node_id("A", "orders/api.py", "handle"),
        to_id="endpoint::A::orders::GET:/orders",
        kind=EdgeKind.CALLS_API,
        confidence=Confidence.AMBIGUOUS,
        evidence=Evidence(file="orders/api.py", line=12, rule="path-fallback:env:REMOTE_URL"),
        props={"base_ref": "env:REMOTE_URL", "method": "GET"},
    )
    inputs.pinned = [PinnedCallInput(pinned, pinned.to_id)]
    result = resolve_repo_links(snap, inputs, EndpointCatalog.from_snapshot(snap))
    edge = next(e for e in result.edges if e.evidence.line == 12)
    assert edge.evidence == pinned.evidence
    assert edge.confidence == pinned.confidence
    fresh = next(e for e in result.edges if e.evidence.line == 10)
    assert fresh.evidence.rule == "path-fallback:env:REMOTE_URL(consistent:1)"


def test_legacy_report_refresh_preserves_distinct_repos_with_identical_message_text():
    from axon.v1.repository_links import reconcile_repositories

    snap = GraphSnapshot()
    for repo in ("A", "X"):
        local = caller(repo=repo)
        inputs = local.link_inputs[repo]
        inputs.prepared = inputs.prepared[1:2]
        inputs.service_map.services[0].calls = []
        snap = publish(snap, local, repo)
    # Simulate the original report-less format, retaining both actual call sites.
    snap.link_inputs.clear()
    snap.link_reports.clear()
    refreshed = reconcile_repositories(snap, GraphSnapshot(), replace_repo_ids=set())
    messages = [refreshed.link_reports[repo].unresolved[0] for repo in ("A", "X")]
    assert messages[0] == messages[1]
    assert refreshed.unresolved == messages
    assert len([e for e in refreshed.edges if e.kind == EdgeKind.CALLS_API]) == 2


def test_unknown_input_format_fails_explicitly():
    from axon.v1.repository_links import resolve_repo_links

    snap = caller()
    inputs = deepcopy(snap.link_inputs["A"])
    inputs.format_version = 2
    with pytest.raises(ValueError, match="link inputs.*2"):
        resolve_repo_links(snap, inputs, EndpointCatalog.from_snapshot(snap))


def test_catalog_changes_refresh_external_node_candidates():
    snap = publish(GraphSnapshot(), caller(), "A")
    first = outbound(snap)[20].to_id
    assert snap.nodes[first].props["candidates"] == "[]"
    changed = publish(snap, repository("C", "ledger", host=True), "C")
    assert changed.nodes[first].props["candidates"] == '["endpoint::C::ledger::GET:/entries"]'
    assert snap.nodes[first].props["candidates"] == "[]"


def test_manual_suppression_uses_catalog_and_removes_unresolved_report():
    from axon.v1.servicemap import Suppression

    a = caller()
    inputs = a.link_inputs["A"]
    inputs.prepared = []
    inputs.service_map.services[0].suppress = [
        Suppression(
            id="ignore-audit", to_repo="B", to_service="ledger", reason="reviewed exclusion"
        )
    ]
    missing = publish(GraphSnapshot(), a, "A")
    assert outbound(missing) == {}
    assert missing.link_reports["A"].unresolved == []
    arrived = publish(missing, repository("B", "ledger"), "B")
    assert outbound(arrived) == {}
    assert any(
        "suppressed:manual#ignore-audit: 1" in d for d in arrived.link_reports["A"].diagnostics
    )


def test_index_repo_attaches_replay_inputs_and_replays_without_checkout(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    from axon.v1.indexer import index_repo
    from axon.v1.repository_links import reconcile_repositories

    (tmp_path / "orders").mkdir()
    (tmp_path / "axon.yaml").write_text("repo: A\nservices:\n  - id: orders\n    roots: [orders]\n")
    (tmp_path / "orders/api.py").write_text(
        "import requests\ndef run():\n    return requests.get('https://ledger.example/entries')\n"
    )
    local, sm = index_repo(str(tmp_path), use_cache=False)
    assert sm.repo == "A"
    assert len(local.link_inputs["A"].prepared) == 1
    assert local.link_reports["A"].coverage["orders"]["outbound_external"] == 1
    retained = GraphSnapshot.model_validate_json(local.model_dump_json())
    shutil.rmtree(tmp_path / "orders")
    (tmp_path / "axon.yaml").unlink()

    def no_read(*args, **kwargs):
        raise AssertionError("replay attempted file IO")

    monkeypatch.setattr(Path, "read_text", no_read)
    result = reconcile_repositories(
        retained, repository("B", "ledger", host=True), replace_repo_ids={"B"}
    )
    assert outbound(result)[3].to_id == "endpoint::B::ledger::GET:/entries"
    assert outbound(result)[3].props["owner_file"] == "orders/api.py"


def test_prepare_and_replay_do_not_mutate_bound_facts_or_retained_configuration():
    from axon.v1.repository_links import prepare_link_inputs, resolve_repo_links

    a, b = repository("A", "orders"), repository("B", "ledger", host=True)
    target = b.nodes.pop("endpoint::B::ledger::GET:/entries")
    target.id = "endpoint::B::ledger::GET:/v1/entries/{id}"
    target.props["path"] = "/v1/entries/{id}"
    b.nodes[target.id] = target
    fact = HttpFact("handle", 9, "GET", "https://ledger.example/v1/entries/42", "requests")
    sm = a.link_inputs["A"].service_map
    prepared = prepare_link_inputs(
        a, sm, [FileFacts("orders/api.py", "python", http_calls=[fact])], {}
    )
    sm.services[0].base_urls.append("https://unrelated.example")
    before = deepcopy(prepared)
    result = resolve_repo_links(a, prepared, EndpointCatalog.from_snapshot(b))
    assert result.edges[0].to_id == target.id
    assert result.edges[0].props["bound"] == '{"id": "42"}'
    assert result.edges[0].evidence.rule == "match:literal-param"
    assert result.edges[0].confidence == Confidence.AMBIGUOUS
    assert fact.base_ref == "" and fact.bound == {}
    assert prepared == before
    assert prepared.service_map.services[0].base_urls == []


def test_undeclared_remote_path_and_operation_remain_diagnostic_only():
    a = caller()
    a.link_inputs["A"].service_map.services[0].clients = []
    a.link_inputs["A"].service_map.services[0].calls = []
    call = a.link_inputs["A"].prepared[0].fact
    call.client_id = call.client_owner = ""
    a.link_inputs["A"].prepared[1].fact.url = "/entries"
    snap = publish(publish(GraphSnapshot(), repository("B", "ledger"), "B"), a, "A")
    for line in (10, 20):
        edge = outbound(snap)[line]
        assert snap.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API
        assert edge.props["candidates"] == '["endpoint::B::ledger::GET:/entries"]'


def test_unresolved_reports_from_unchanged_repositories_survive_publication():
    a = publish(GraphSnapshot(), caller(), "A")
    old_report = deepcopy(a.link_reports["A"])
    result = publish(a, repository("D", "other", endpoint=False), "D")
    assert result.link_reports["A"] == old_report
    assert result.unresolved == old_report.unresolved


@pytest.mark.parametrize("order", [("A", "X"), ("X", "A")])
def test_same_placeholder_different_declared_targets_do_not_share_suppression(order):
    from axon.v1.servicemap import Suppression

    repos = {"A": caller(), "X": caller(repo="X", target_repo="C")}
    for repo, snap in repos.items():
        inputs = snap.link_inputs[repo]
        inputs.prepared = inputs.prepared[:1]
        inputs.service_map.services[0].calls = []
        inputs.service_map.services[0].suppress = [
            Suppression(
                id="ignore-b", to_repo="B", to_service="ledger", reason="reviewed exclusion"
            )
        ]
    snap = GraphSnapshot()
    for repo in order:
        snap = publish(snap, repos[repo], repo)
    edges = [e for e in snap.edges if e.kind == EdgeKind.CALLS_API]
    assert len(edges) == 1
    assert snap.nodes[edges[0].from_id].repo_id == "X"
    assert edges[0].props["target_repo"] == "C"
    assert edges[0].props["target_service"] == "ledger"
    assert "target_repo" not in snap.nodes[edges[0].to_id].props
    assert snap.link_reports["A"].unresolved == []
    assert len(snap.link_reports["X"].unresolved) == 1
    arrived = publish(snap, repository("C", "ledger"), "C")
    assert outbound(arrived)[10].to_id == "endpoint::C::ledger::GET:/entries"


def test_client_source_suppression_counts_survive_missing_symbol():
    from axon.v1.repository_links import prepare_link_inputs, resolve_repo_links
    from axon.v1.servicemap import ClientDecl

    a = repository("A", "orders")
    sm = a.link_inputs["A"].service_map
    sm.services[0].clients = [ClientDecl(id="generated", paths=["generated"])]
    facts = FileFacts(
        "generated/api.py",
        "python",
        suppressed_calls=2,
        http_calls=[HttpFact("module", 3, via="generated-client", call_name="getValue")],
    )
    inputs = prepare_link_inputs(a, sm, [facts], {})
    result = resolve_repo_links(a, inputs, EndpointCatalog.from_snapshot(a))
    assert result.report.diagnostics == ["orders: client-source calls suppressed: 3"]


def test_published_host_collision_is_not_overridden_by_consensus():
    from axon.v1.link_inputs import PreparedCallInput

    a = repository("A", "orders")
    b = repository("B", "ledger", host=True)
    c = repository("C", "ledger", host=True)
    for line, path in [(10, "/one"), (20, "/two"), (30, "/entries")]:
        a.link_inputs["A"].prepared.append(
            PreparedCallInput(
                symbol_node_id("A", "orders/api.py", "handle"),
                "orders/api.py",
                "service",
                "orders",
                0,
                "",
                HttpFact("handle", line, "GET", "https://ledger.example" + path, "requests"),
            )
        )
        if path != "/entries":
            eid = endpoint_node_id("B", "ledger", "GET", path)
            b.add_node(
                GraphNode(
                    id=eid,
                    kind=NodeKind.API_ENDPOINT,
                    repo_id="B",
                    service_id="ledger",
                    props={"method": "GET", "path": path},
                )
            )
    snap = publish(publish(publish(GraphSnapshot(), a, "A"), b, "B"), c, "C")
    assert snap.nodes[outbound(snap)[30].to_id].kind == NodeKind.EXTERNAL_API
    assert outbound(snap)[30].evidence.rule == "unresolved:path-ambiguous"


def test_manual_link_missing_remote_target_can_be_suppressed():
    from axon.v1.servicemap import ManualLink, Suppression

    a = caller()
    inputs = a.link_inputs["A"]
    inputs.prepared = inputs.prepared[:1]
    service = inputs.service_map.services[0]
    service.calls = []
    service.links = [
        ManualLink(
            id="redirect",
            call_name="ledger_sdk.getEntries",
            to_repo="C",
            to_service="ledger",
            to_method="GET",
            to_path="/entries",
        )
    ]
    service.suppress = [
        Suppression(id="ignore-c", to_repo="C", to_service="ledger", reason="excluded")
    ]
    result = publish(GraphSnapshot(), a, "A")
    assert outbound(result) == {}
    assert result.link_reports["A"].unresolved == []


@pytest.mark.parametrize("caller_repo", ["", "A", "C"])
def test_private_link_wrapper_uses_explicit_repo_without_adopting_foreign_callers(caller_repo):
    from axon.v1.indexer import _link_http

    snap = repository("A", "orders")
    snap.nodes.pop(repo_node_id("A"))
    caller_id = symbol_node_id("A", "orders/api.py", "handle")
    snap.nodes[caller_id].repo_id = caller_repo
    fact = HttpFact("handle", 7, "GET", "/orders", "requests")
    _link_http(
        snap,
        "A",
        ServiceMap(),
        [FileFacts("orders/api.py", "python", http_calls=[fact])],
    )
    assert snap.nodes[caller_id].repo_id == caller_repo
    assert len(snap.link_inputs["A"].prepared) == (0 if caller_repo == "C" else 1)
    assert len(outbound(snap)) == (0 if caller_repo == "C" else 1)
    if caller_repo != "C":
        assert outbound(snap)[7].to_id == "endpoint::A::orders::GET:/orders"


def test_preparation_requires_unambiguous_repo_unless_explicit():
    from axon.v1.repository_links import prepare_link_inputs

    snap = GraphSnapshot()
    with pytest.raises(ValueError, match="exactly one repository"):
        prepare_link_inputs(snap, ServiceMap(), [], {})
    assert prepare_link_inputs(snap, ServiceMap(), [], {}, repo_id="A").repo_id == "A"
    foreign = repository("C", "ledger")
    with pytest.raises(ValueError, match="source repository A"):
        prepare_link_inputs(foreign, ServiceMap(), [], {}, repo_id="A")


def test_replacement_scope_ignores_foreign_candidate_structure_and_inputs():
    current = publish(GraphSnapshot(), repository("B", "ledger", host=True), "B")
    original = current.model_dump(mode="json")
    incoming = caller()
    foreign = repository("B", "ledger", endpoint=False)
    incoming.nodes.update(foreign.nodes)
    incoming.link_inputs.update(foreign.link_inputs)
    target_id = "endpoint::B::ledger::GET:/entries"
    incoming.nodes[target_id] = current.nodes[target_id].model_copy(deep=True)
    incoming.nodes[target_id].props["path"] = "/foreign-replacement"
    combined = publish(current, incoming, "A")
    assert combined.nodes[target_id].props["path"] == "/entries"
    assert combined.link_inputs["B"] == current.link_inputs["B"]
    assert outbound(combined)[20].to_id == target_id
    assert current.model_dump(mode="json") == original
