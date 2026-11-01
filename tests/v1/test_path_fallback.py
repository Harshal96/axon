"""Symbolic URL matching retains uncertainty and source evidence."""

import json

import pytest

from axon.v1.facts import FileFacts, HttpFact
from axon.v1.graph import GraphSnapshot
from axon.v1.indexer import _link_http
from axon.v1.linker import EndpointIndex, base_url_suggestions, resolve_http
from axon.v1.models import Confidence, EdgeKind, GraphNode, NodeKind, endpoint_node_id
from axon.v1.servicemap import IndexOptions, ServiceDecl, ServiceMap


def endpoint(service, path, method="POST", **props):
    return GraphNode(
        id=endpoint_node_id("sample", service, method, path),
        kind=NodeKind.API_ENDPOINT,
        repo_id="sample",
        service_id=service,
        props={"method": method, "path": path, **props},
    )


def snapshot(*endpoints):
    snap = GraphSnapshot()
    for ep in endpoints:
        snap.add_node(ep)
    snap.add_node(GraphNode(id="symbol::sample::app.py::run", kind=NodeKind.SYMBOL))
    return snap


def call(path="/internal/v3/items", ref="env:REMOTE_URL", method="POST", line=4):
    return HttpFact(caller="run", line=line, method=method, url="{" + ref + "}" + path)


@pytest.mark.parametrize("ref", ["env:REMOTE_URL", "setting:REMOTE_URL", "attr:client.base"])
@pytest.mark.parametrize("path", ["/internal/v3/items", "internal/v3/items/"])
def test_symbolic_base_unique_path_fallback(ref, path):
    ep = endpoint("remote", "/internal/v3/items")
    fact = call(path, ref)
    assert resolve_http(fact, EndpointIndex(snapshot(ep)), ServiceMap(), "sample") == (
        ep.id,
        Confidence.AMBIGUOUS,
        "path-fallback:" + ref,
    )
    assert fact.base_ref == ref


def test_symbolic_base_spec_and_gateway_alias():
    ep = endpoint("remote", "/service/api/internal/v3/items", base_path="/service/api")
    assert resolve_http(call(), EndpointIndex(snapshot(ep)), ServiceMap(), "sample")[0] == ep.id
    gateway = endpoint("remote", "/internal/v3/items")
    assert (
        resolve_http(
            call("/api/internal/v3/items"), EndpointIndex(snapshot(gateway)), ServiceMap(), "sample"
        )[0]
        == gateway.id
    )


def test_fallback_cannot_cross_verb_or_bind_two_literals():
    for ep in [
        endpoint("remote", "/internal/v3/items", "GET"),
        endpoint("remote", "/internal/{version}/{id}"),
    ]:
        assert resolve_http(call(), EndpointIndex(snapshot(ep)), ServiceMap(), "sample") is None


def test_declared_scope_wins_and_cannot_fall_through():
    a, b = endpoint("first", "/internal/v3/items"), endpoint("second", "/internal/v3/items")
    sm = ServiceMap(services=[ServiceDecl(id="first", base_urls=["env:REMOTE_URL"])])
    assert resolve_http(call(), EndpointIndex(snapshot(a, b)), sm, "sample") == (
        a.id,
        Confidence.INFERRED,
        "base-url:env:REMOTE_URL",
    )
    assert resolve_http(call("/different"), EndpointIndex(snapshot(b)), sm, "sample") is None


def test_fallback_ambiguity_records_candidate_ids_and_base():
    first, second = (
        endpoint("first", "/internal/v3/items"),
        endpoint("second", "/internal/v3/items"),
    )
    snap = snapshot(first, second)
    _link_http(
        snap,
        "sample",
        ServiceMap(),
        [FileFacts(path="app.py", language="python", http_calls=[call()])],
    )
    edge = snap.edges[0]
    assert edge.evidence.rule == "unresolved:path-ambiguous"
    assert edge.props["base_ref"] == "env:REMOTE_URL"
    assert json.loads(snap.nodes[edge.to_id].props["candidates"]) == sorted([first.id, second.id])


def test_fallback_agreement_promotes_with_count_and_evidence():
    snap = snapshot(endpoint("remote", "/internal/v3/items"))
    _link_http(
        snap,
        "sample",
        ServiceMap(),
        [FileFacts(path="app.py", language="python", http_calls=[call(line=n) for n in [4, 5, 6]])],
    )
    assert len(snap.edges) == 3
    assert all(e.confidence == Confidence.INFERRED for e in snap.edges)
    assert all(e.evidence.rule == "path-fallback:env:REMOTE_URL(consistent:3)" for e in snap.edges)
    assert [(e.evidence.file, e.evidence.line) for e in snap.edges] == [
        ("app.py", n) for n in [4, 5, 6]
    ]
    assert all(e.props["base_ref"] == "env:REMOTE_URL" for e in snap.edges)


def test_fallback_conflict_stays_ambiguous_with_diagnostic():
    snap = snapshot(
        endpoint("first", "/internal/v3/items"), endpoint("second", "/internal/v3/other")
    )
    _link_http(
        snap,
        "sample",
        ServiceMap(),
        [
            FileFacts(
                path="app.py",
                language="python",
                http_calls=[call(), call("/internal/v3/other", line=5)],
            )
        ],
    )
    assert all(e.confidence == Confidence.AMBIGUOUS for e in snap.edges)
    assert any("env:REMOTE_URL" in d and "first" in d and "second" in d for d in snap.diagnostics)
    assert base_url_suggestions(snap) == []


def test_base_url_suggestions_are_advisory_with_counts_and_samples():
    snap = snapshot(endpoint("remote", "/internal/v3/items"))
    sm = ServiceMap(services=[ServiceDecl(id="remote")])
    before = sm.model_dump()
    _link_http(
        snap,
        "sample",
        sm,
        [
            FileFacts(
                path="app.py", language="python", http_calls=[call(line=n) for n in range(4, 8)]
            )
        ],
    )
    assert base_url_suggestions(snap) == [
        "suggested base_urls: [env:REMOTE_URL] on service remote: 4 call site(s); "
        "app.py:4, app.py:5, app.py:6; review before declaring"
    ]
    assert sm.model_dump() == before


def test_suffix_fallback_is_opt_in_and_requires_three_segments_and_method():
    ep = endpoint("remote", "/gateway/internal/v3/items")
    snap = snapshot(ep)
    assert resolve_http(call(), EndpointIndex(snap), ServiceMap(), "sample") is None
    sm = ServiceMap(index=IndexOptions(path_suffix_fallback=True))
    assert resolve_http(call(), EndpointIndex(snap), sm, "sample") == (
        ep.id,
        Confidence.AMBIGUOUS,
        "path-fallback:suffix",
    )
    assert resolve_http(call(method=""), EndpointIndex(snap), sm, "sample") is None
    assert resolve_http(call("/v3/items"), EndpointIndex(snap), sm, "sample") is None
    assert IndexOptions().fingerprint() != sm.index.fingerprint()


def test_suffix_ambiguity_stays_external_and_suffix_links_never_promote():
    ep = endpoint("remote", "/gateway/internal/v3/items")
    sm = ServiceMap(index=IndexOptions(path_suffix_fallback=True))
    snap = snapshot(ep)
    _link_http(
        snap, "sample", sm, [FileFacts(path="app.py", language="python", http_calls=[call()])]
    )
    assert snap.edges[0].confidence == Confidence.AMBIGUOUS
    snap = snapshot(ep, endpoint("other", "/proxy/internal/v3/items"))
    _link_http(
        snap, "sample", sm, [FileFacts(path="app.py", language="python", http_calls=[call()])]
    )
    edge = next(e for e in snap.edges if e.kind == EdgeKind.CALLS_API)
    assert edge.evidence.rule == "unresolved:path-ambiguous"
    assert len(json.loads(snap.nodes[edge.to_id].props["candidates"])) == 2


def test_consensus_resolves_only_ambiguous_candidate_in_agreed_service():
    first = endpoint("first", "/v1/items")
    second = endpoint("second", "/v1/items")
    snap = snapshot(first, second, endpoint("first", "/v1/a"), endpoint("first", "/v1/b"))
    _link_http(
        snap,
        "sample",
        ServiceMap(),
        [
            FileFacts(
                path="app.py",
                language="python",
                http_calls=[
                    call("/v1/a", line=1),
                    call("/v1/b", line=2),
                    call("/v1/items", line=3),
                ],
            )
        ],
    )
    edge = next(e for e in snap.edges if e.evidence.line == 3)
    assert edge.to_id == first.id
    assert edge.confidence == Confidence.AMBIGUOUS
    assert edge.evidence.rule == "path-fallback:env:REMOTE_URL(consensus:2)"
    assert json.loads(edge.props["candidates"]) == sorted([first.id, second.id])
    assert not snap.unresolved
    assert not snap.by_kind(NodeKind.EXTERNAL_API)


def test_catchall_and_parameter_only_never_path_candidates():
    snap = snapshot(
        endpoint("remote", "/{path:.*}"), endpoint("remote", "/{id}"), endpoint("remote", "/")
    )
    index = EndpointIndex(snap)
    assert index.by_method_path("POST", "/{var}") == []
    _link_http(
        snap,
        "sample",
        ServiceMap(),
        [FileFacts(path="app.py", language="python", http_calls=[call("/{var}")])],
    )
    assert snap.edges[0].evidence.rule == "unresolved:dynamic-path"
    assert json.loads(snap.nodes[snap.edges[0].to_id].props["candidates"]) == []


def test_literal_parameter_match_is_strict_and_retains_binding():
    ep = endpoint("remote", "/v1/items/{id}/flag/name/{name}")
    snap = snapshot(ep)
    _link_http(
        snap,
        "sample",
        ServiceMap(),
        [
            FileFacts(
                path="app.py",
                language="python",
                http_calls=[call("/v1/items/{id}/flag/name/ACTIVE")],
            )
        ],
    )
    edge = snap.edges[0]
    assert edge.to_id == ep.id
    assert edge.evidence.rule == "match:literal-param"
    assert edge.confidence == Confidence.AMBIGUOUS
    assert json.loads(edge.props["bound"]) == {"name": "ACTIVE"}
    assert (
        resolve_http(
            call("/v1/items/ONE/flag/name/TWO"), EndpointIndex(snap), ServiceMap(), "sample"
        )
        is None
    )


def test_attribute_consensus_never_crosses_unrelated_class_instances():
    a = endpoint("first", "/v1/shared")
    b = endpoint("second", "/v1/shared")
    snap = snapshot(a, b, endpoint("first", "/v1/a"), endpoint("first", "/v1/b"))
    from axon.v1.models import symbol_node_id

    for file, cls in [("one.py", "One"), ("two.py", "Two")]:
        snap.add_node(
            GraphNode(
                id=symbol_node_id("sample", file, cls + ".run"),
                kind=NodeKind.SYMBOL,
                repo_id="sample",
                service_id="app",
                file=file,
                props={"qualified": cls + ".run"},
            )
        )
    _link_http(
        snap,
        "sample",
        ServiceMap(),
        [
            FileFacts(
                path="one.py",
                language="python",
                http_calls=[
                    HttpFact("One.run", 1, "POST", "{attr:self.base_url}/v1/a"),
                    HttpFact("One.run", 2, "POST", "{attr:self.base_url}/v1/b"),
                ],
            ),
            FileFacts(
                path="two.py",
                language="python",
                http_calls=[HttpFact("Two.run", 3, "POST", "{attr:self.base_url}/v1/shared")],
            ),
        ],
    )
    edge = next(e for e in snap.edges if e.evidence.file == "two.py")
    assert edge.evidence.rule == "unresolved:path-ambiguous"
