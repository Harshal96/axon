"""Shared endpoint candidates are scoped before matching."""

import pytest

from axon.v1.endpoint_catalog import EndpointCatalog
from axon.v1.graph import GraphSnapshot
from axon.v1.link_inputs import RepoLinkInputs
from axon.v1.models import Confidence, GraphNode, NodeKind, endpoint_node_id
from axon.v1.servicemap import ServiceDecl, ServiceMap


def endpoint(repo, path="/v1/items/{id}"):
    return GraphNode(
        id=endpoint_node_id(repo, "api", "GET", path),
        kind=NodeKind.API_ENDPOINT,
        repo_id=repo,
        service_id="api",
        props={"method": "GET", "path": path, "operation_id": "list_items", "source": "spec"},
    )


def catalog(*nodes, bases=None):
    snap = GraphSnapshot()
    for node in nodes:
        snap.add_node(node)
    for repo, urls in (bases or {}).items():
        snap.link_inputs[repo] = RepoLinkInputs(
            repo, ServiceMap(services=[ServiceDecl(id="api", base_urls=urls)])
        )
    return EndpointCatalog.from_snapshot(snap)


def test_scope_precedes_duplicate_operation_and_literal_binding():
    b, c = endpoint("B"), endpoint("C")
    index = catalog(b, c)
    scope = frozenset({("B", "api")})
    assert index.by_operation("list_items_1", scopes=scope) == []
    assert index.by_operation("list_items_1", scopes=index.scopes) == []
    assert index.literal_parameter("GET", "/v1/items/123", scopes=scope) == [(b, {"id": "123"})]


def test_construction_and_lookup_do_not_change_input_nodes():
    b = endpoint("B")
    before = b.model_dump_json()
    index = catalog(b)
    index.by_method_path("GET", "/v1/items/{id}", scopes=frozenset({("B", "api")}))
    assert b.model_dump_json() == before
    returned = index.get_node(b.id)
    assert returned is not None
    returned.props["path"] = "/changed"
    assert index.get_node(b.id).props["path"] == "/v1/items/{id}"
    scope = frozenset({("B", "api")})
    matched = index.by_method_path("GET", "/v1/items/{id}", scopes=scope)
    matched[0].props["path"] = "/changed"
    assert index.by_method_path("GET", "/v1/items/{id}", scopes=scope)[0] == b
    bound = index.literal_parameter("GET", "/v1/items/123", scopes=scope)
    bound[0][1]["id"] = "changed"
    assert index.literal_parameter("GET", "/v1/items/123", scopes=scope) == [(b, {"id": "123"})]


def test_literal_authority_port_and_longest_segment_prefix():
    index = catalog(
        endpoint("B"),
        endpoint("C"),
        bases={"B": ["https://EXAMPLE.com:443/api"], "C": ["https://example.com/api/v1"]},
    )
    assert index.literal_base_scopes("https://example.com/api/v1/items") == frozenset(
        {("C", "api")}
    )
    assert index.literal_base_scopes("https://example.com/apian") == frozenset()
    assert index.literal_base_scopes("https://example.com:8443/api/v1") == frozenset()
    assert index.literal_base_scopes("https://example.com.evil/api/v1") == frozenset()
    assert index.literal_base_scopes("http://example.com:443/api/v1") == frozenset()


def test_equal_literal_bases_compete():
    index = catalog(
        endpoint("B"),
        endpoint("C"),
        bases={"B": ["https://example.com/api"], "C": ["https://example.com/api"]},
    )
    assert index.literal_base_scopes("https://example.com/api/items") == frozenset(
        {("B", "api"), ("C", "api")}
    )


def test_shared_catalog_weak_fallback_stays_local_and_literal_host_can_cross():
    from axon.v1.facts import HttpFact
    from axon.v1.linker import resolve_http

    b = endpoint("B", "/v1/items")
    index = catalog(b, bases={"B": ["https://remote.example"]})
    assert (
        resolve_http(HttpFact("run", 1, "GET", "{env:BASE}/v1/items"), index, ServiceMap(), "A")
        is None
    )
    assert (
        resolve_http(
            HttpFact("run", 1, "GET", "https://remote.example/v1/items"), index, ServiceMap(), "A"
        )[0]
        == b.id
    )
    assert (
        resolve_http(
            HttpFact("run", 1, "GET", "https://other.example/v1/items"), index, ServiceMap(), "A"
        )
        is None
    )


@pytest.mark.parametrize(
    "url,path,via,call_name",
    [
        ("https://internet.example/v1/items", "/v1/items", "requests", ""),
        ("https://internet.example/v1/items/123", "/v1/items/{id}", "requests", ""),
        (
            "https://internet.example/v1/items/{id}",
            "/v1/items/{id}",
            "generated-source",
            "list_items",
        ),
        ("https://internet.example/missing", "/v1/items", "generated-client", "list_items"),
        ("https://internet.example", "/v1/items", "generated-client", "list_items"),
    ],
)
def test_unbound_absolute_authority_cannot_use_local_path_parameter_or_operation(
    url, path, via, call_name
):
    from axon.v1.facts import HttpFact
    from axon.v1.linker import resolve_http

    index = catalog(endpoint("A", path))
    fact = HttpFact("run", 1, "GET", url, via, call_name=call_name)
    assert resolve_http(fact, index, ServiceMap(), "A") is None
    assert fact.bound == {}


def test_declared_literal_local_host_allows_self_call():
    from axon.v1.facts import HttpFact
    from axon.v1.linker import resolve_http

    local = endpoint("A", "/v1/items")
    index = catalog(local, bases={"A": ["https://local.example"]})
    assert resolve_http(
        HttpFact("run", 1, "GET", "https://local.example/v1/items"),
        index,
        ServiceMap(),
        "A",
    ) == (local.id, Confidence.INFERRED, "base-url:local.example")


@pytest.mark.parametrize("binding", ["published-service", "caller-client"])
def test_declared_literal_authority_allows_template_path_without_symbolic_host_binding(binding):
    from axon.v1.facts import HttpFact
    from axon.v1.linker import resolve_http

    local = endpoint("A")
    index = catalog(
        local, bases={"A": ["https://local.example"]} if binding == "published-service" else {}
    )
    sm = ServiceMap.model_validate(
        {
            "services": [
                {
                    "id": "app",
                    "clients": [
                        {
                            "id": "local",
                            "base_urls": ["https://local.example"],
                            "target_service": "api",
                        }
                    ],
                }
            ]
        }
    )
    assert resolve_http(
        HttpFact("run", 1, "GET", "https://local.example/v1/items/{id}?token={token}"),
        index,
        sm if binding == "caller-client" else ServiceMap(),
        "A",
    ) == (local.id, Confidence.INFERRED, "base-url:local.example")
    assert index.literal_base_scopes("https://{env:HOST}/v1/items/{id}") == frozenset()
    assert index.literal_base_scopes("https://unknown.example/v1/items/{id}") == frozenset()
    dynamic_base = catalog(local, bases={"A": ["https://local.example/{prefix}"]})
    assert dynamic_base.literal_base_scopes("https://local.example/{prefix}/items") == frozenset()


@pytest.mark.parametrize(
    "url,rule,confidence",
    [
        ("/v1/items", "match:method+path", Confidence.INFERRED),
        ("{env:BASE}/v1/items", "path-fallback:env:BASE", Confidence.AMBIGUOUS),
        ("{setting:BASE}/v1/items", "path-fallback:setting:BASE", Confidence.AMBIGUOUS),
        ("{attr:client.base}/v1/items", "path-fallback:attr:client.base", Confidence.AMBIGUOUS),
        ("", "match:operationId", Confidence.INFERRED),
    ],
)
def test_relative_symbolic_and_urlless_fallback_remain_caller_local(url, rule, confidence):
    from axon.v1.facts import HttpFact
    from axon.v1.linker import resolve_http

    local, foreign = endpoint("A", "/v1/items"), endpoint("B", "/v1/items")
    result = resolve_http(
        HttpFact("run", 1, "GET", url, "generated-client", call_name="list_items"),
        catalog(local, foreign),
        ServiceMap(),
        "A",
    )
    assert result == (local.id, confidence, rule)


def test_explicit_client_base_missing_target_never_uses_published_host():
    from axon.v1.facts import HttpFact
    from axon.v1.linker import resolve_http

    c = endpoint("C", "/v1/items")
    index = catalog(c, bases={"C": ["https://remote.example"]})
    sm = ServiceMap.model_validate(
        {
            "services": [
                {
                    "id": "app",
                    "clients": [
                        {
                            "id": "remote",
                            "base_urls": ["https://remote.example"],
                            "target_repo": "B",
                            "target_service": "api",
                        }
                    ],
                }
            ]
        }
    )
    assert (
        resolve_http(HttpFact("run", 1, "GET", "https://remote.example/v1/items"), index, sm, "A")
        is None
    )


def test_short_caller_service_base_does_not_broaden_published_longest_prefix():
    from axon.v1.facts import HttpFact
    from axon.v1.linker import resolve_http

    a, c = endpoint("A", "/v1/items"), endpoint("C", "/v1/items")
    index = catalog(
        a, c, bases={"A": ["https://remote.example"], "C": ["https://remote.example/v1"]}
    )
    sm = ServiceMap(services=[ServiceDecl(id="api", base_urls=["https://remote.example"])])
    result = resolve_http(
        HttpFact("run", 1, "GET", "https://remote.example/v1/items"), index, sm, "A"
    )
    assert result == (c.id, Confidence.INFERRED, "base-url:remote.example")


def test_suppression_defaults_to_caller_repo_and_explicit_repo_removes_foreign():
    from axon.v1.manual_corrections import suppress_edges
    from axon.v1.models import EdgeKind, GraphEdge
    from axon.v1.servicemap import Suppression

    snap = GraphSnapshot()
    caller = GraphNode(id="caller", kind=NodeKind.SYMBOL, repo_id="A", service_id="app")
    snap.add_node(caller)
    for repo in ["A", "B"]:
        target = endpoint(repo)
        snap.add_node(target)
        snap.add_edge(GraphEdge(from_id=caller.id, to_id=target.id, kind=EdgeKind.CALLS_API))
    sm = ServiceMap(
        services=[
            ServiceDecl(
                id="app", suppress=[Suppression(id="s", to_service="api", reason="reviewed")]
            )
        ]
    )
    suppress_edges(snap, "A", sm)
    assert [snap.nodes[e.to_id].repo_id for e in snap.edges] == ["B"]
    sm.services[0].suppress[0].to_repo = "B"
    suppress_edges(snap, "A", sm)
    assert snap.edges == []
    external = GraphNode(id="external", kind=NodeKind.EXTERNAL_API)
    snap.add_node(external)
    snap.add_edge(GraphEdge(from_id=caller.id, to_id=external.id, kind=EdgeKind.CALLS_API))
    sm.services[0].suppress[0].to_repo = ""
    sm.services[0].suppress[0].to_service = ""
    suppress_edges(snap, "A", sm)
    assert snap.edges == []
