"""Operation-to-field contract, independent of Apollo's host-language syntax."""

import json
from dataclasses import replace

import pytest
from graphql import build_schema

from axon.v1.graph import GraphSnapshot
from axon.v1.graphql_calls import link_graphql_calls
from axon.v1.graphql_inputs import DocumentSource, InvocationInput
from axon.v1.graphql_schema import ProviderSchema
from axon.v1.models import Confidence, EdgeKind, GraphNode, NodeKind, graphql_endpoint_node_id
from axon.v1.servicemap import ServiceDecl, ServiceMap

SCHEMA = """
schema { query: Store mutation: Write }
interface Status { status: String }
type Store implements Status { status: String order(id: ID!): Order }
type Order implements Status { id: ID! status: String }
type Write { order: String }
"""


def provider(service: str) -> ProviderSchema:
    endpoints = {
        coordinate: graphql_endpoint_node_id("r", service, coordinate)
        for coordinate in ("Store.order", "Store.status", "Write.order")
    }
    return ProviderSchema(
        service_id=service,
        schema=build_schema(SCHEMA),
        roots={"query": "Store", "mutation": "Write"},
        endpoints=endpoints,
        mounts={"/graphql"},
        dependencies={f"{service}/schema.gql"},
    )


@pytest.fixture
def orders() -> ProviderSchema:
    return provider("orders")


def call(text: str = "{ status }") -> InvocationInput:
    return InvocationInput(
        caller_id="symbol::r::ui/page.ts::load",
        file="ui/page.ts",
        line=20,
        column=5,
        kind="query",
        uri="https://orders.test/graphql",
        documents=[DocumentSource("ui/doc.ts", text, 8, 11)],
        dependencies=["ui/config.ts"],
    )


def link(
    providers: list[ProviderSchema], *calls: InvocationInput, origin: str = "https://orders.test"
) -> GraphSnapshot:
    snap = GraphSnapshot()
    sm = ServiceMap(services=[ServiceDecl(id=p.service_id, base_urls=[origin]) for p in providers])
    for p in providers:
        for endpoint in p.endpoints.values():
            snap.add_node(
                GraphNode(
                    id=endpoint,
                    kind=NodeKind.API_ENDPOINT,
                    service_id=p.service_id,
                    props={"protocol": "graphql"},
                )
            )
    link_graphql_calls(snap, "r", sm, {p.service_id: p for p in providers}, list(calls))
    snap.dedupe_edges()
    return snap


def test_repeated_selections_aggregate_with_each_location_and_conditional(orders: ProviderSchema):
    invocation = call(
        'query Page($show: Boolean!) { current: order(id:"1") { id } ...Summary '
        "... on Store @include(if:$show) { maybe: status } }"
    )
    invocation.documents.append(
        DocumentSource("ui/summary.gql", "fragment Summary on Status { status }")
    )
    snap = link([orders], invocation, replace(invocation, column=48))
    assert len(snap.edges) == 4
    assert not snap.unresolved
    assert {e.to_id for e in snap.edges} == {
        orders.endpoints["Store.order"],
        orders.endpoints["Store.status"],
    }
    for edge in snap.edges:
        assert edge.confidence == Confidence.INFERRED
        assert edge.kind == EdgeKind.CALLS_API
        assert (edge.evidence.file, edge.evidence.line, edge.evidence.rule) == (
            "ui/page.ts",
            20,
            "graphql:apollo-field",
        )
        assert json.loads(edge.props["operation_location"]) == {
            "file": "ui/doc.ts",
            "line": 8,
            "column": 11,
        }
        assert set(json.loads(edge.props["graphql_dependencies"])) >= {
            "orders/schema.gql",
            "ui/config.ts",
            "ui/doc.ts",
            "ui/summary.gql",
        }
        assert edge.props["operation_name"] == "Page"
        assert json.loads(edge.props["graphql_client_dependencies"]) == [
            "ui/config.ts",
            "ui/doc.ts",
            "ui/summary.gql",
        ]
        selections = json.loads(edge.props["selections"])
        if edge.to_id.endswith("Store.status"):
            assert {(s["file"], s["alias"], s["conditional"]) for s in selections} == {
                ("ui/doc.ts", "maybe", True),
                ("ui/summary.gql", "", False),
            }
        else:
            assert [(s["file"], s["column"], s["alias"]) for s in selections] == [
                ("ui/doc.ts", 41, "current")
            ]
    assert {e.props["column"] for e in snap.edges} == {"5", "48"}


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ("query Page { status ...Lost }", "invalid-document"),
        ("{ ...A } fragment A on Store { ...A }", "invalid-document"),
        ("query Page { status } query Other { status }", "ambiguous-operation"),
        ("mutation { order }", "operation-kind"),
        ("{ status order { id } }", "invalid-document"),
        ("{ status order(id: 1) { missing } }", "invalid-document"),
        ("query Wrong($unused: Boolean) { status }", "invalid-document"),
        ("{ status } fragment NeverUsed on Store { status }", "invalid-document"),
        ("{ status order(id: 1) }", "invalid-document"),
        ("{ status unknown @skip(if:true) }", "invalid-document"),
    ],
)
def test_invalid_call_is_one_external_without_partial_fields(
    orders: ProviderSchema, document: str, reason: str
):
    snap = link([orders], call(document))
    (edge,) = snap.edges
    assert snap.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API
    assert edge.evidence.rule == f"unresolved:graphql-{reason}"
    assert len(snap.unresolved) == 1
    if reason == "invalid-document":
        assert any("ui/doc.ts:8:" in diagnostic for diagnostic in snap.diagnostics)


def test_mixed_local_and_server_variable_ownership(orders: ProviderSchema):
    snap = link([orders], call("query Page($x: Int) { local(x:$x) @client status }"))
    assert [e.to_id for e in snap.edges] == [orders.endpoints["Store.status"]]
    assert not snap.unresolved
    filtered = call(
        "{ __typename status @skip(if:true) ...Client } fragment Client on Store { local @client }"
    )
    assert not link([orders], filtered).edges
    assert not link([orders], replace(call(), documents=[], cache_only=True)).edges


@pytest.mark.parametrize(
    "document",
    [
        '{ current: order(id: "1") { local @client } }',
        '{ current: order(id: "1") { ...Details } } fragment Details on Order { local @client }',
    ],
)
def test_remote_parent_with_client_only_children(orders: ProviderSchema, document: str):
    snap = link([orders], call(document))
    (edge,) = snap.edges
    assert edge.to_id == orders.endpoints["Store.order"]
    assert json.loads(edge.props["selections"]) == [
        {"alias": "current", "file": "ui/doc.ts", "line": 8, "column": 13, "conditional": False}
    ]
    assert not snap.unresolved


def test_client_only_root_and_invalid_scalar_parent(orders: ProviderSchema):
    assert not link([orders], call("{ local @client { nested } }")).edges
    snap = link([orders], call("{ status { local @client } }"))
    (edge,) = snap.edges
    assert edge.evidence.rule == "unresolved:graphql-invalid-document"
    assert "ui/doc.ts:8:20:" in edge.evidence.detail
    assert "type 'String' has no subfields" in edge.evidence.detail


def test_named_operation_and_nested_type_condition(orders: ProviderSchema):
    request = call(
        "query One { status } "
        "query Two { ... on Status { ... on Order { status } } order(id:1){id} }"
    )
    snap = link([orders], replace(request, operation_name="Two"))
    assert [e.to_id for e in snap.edges] == [orders.endpoints["Store.order"]]


def test_named_operation_validates_all_server_definitions(orders: ProviderSchema):
    request = call("query One { status } query Two { missing local @client }")
    snap = link([orders], replace(request, operation_name="One"))
    assert [e.evidence.rule for e in snap.edges] == ["unresolved:graphql-invalid-document"]
    assert any("missing" in error for error in snap.diagnostics)


def test_fragment_used_only_in_local_subtree(orders: ProviderSchema):
    request = call(
        "query Q($arg: String) { status settings @client { ...Local } } "
        "fragment Local on ClientSettings { read(key: $arg) }"
    )
    assert [e.to_id for e in link([orders], request).edges] == [orders.endpoints["Store.status"]]


def test_document_and_fragment_locations_on_validation_error(orders: ProviderSchema):
    request = call("{ ...Details }")
    request.documents.append(
        DocumentSource("ui/parts.ts", "fragment Details on Store {\n  missing\n}", 20, 35)
    )
    snap = link([orders], request)
    assert len(snap.unresolved) == 1
    assert any("ui/parts.ts:21:3" in diagnostic for diagnostic in snap.diagnostics)


def test_scope_is_required_even_with_unique_field(orders: ProviderSchema):
    for uri in ("https://orders.test/wrong", "https://other.test/graphql", "/graphql", ""):
        snap = link([orders], replace(call(), uri=uri))
        (edge,) = snap.edges
        assert edge.evidence.rule == "unresolved:graphql-unknown-target"
        assert json.loads(edge.props["candidates"]) == []
    archive = provider("archive")
    snap = link([orders, archive], call())
    (ambiguous,) = snap.edges
    assert ambiguous.evidence.rule == "unresolved:graphql-ambiguous-target"
    assert set(json.loads(ambiguous.props["candidates"])) == {
        orders.endpoints["Store.status"],
        archive.endpoints["Store.status"],
    }


def test_unresolved_sites_are_distinct(orders: ProviderSchema):
    first = replace(call(), documents=[])
    snap = link([orders], first, replace(first, column=16))
    assert len(snap.unresolved) == len({e.to_id for e in snap.edges}) == 2
    assert {e.evidence.rule for e in snap.edges} == {"unresolved:graphql-dynamic-document"}


def test_bounded_transitive_fragments(orders: ProviderSchema):
    chain = [f"fragment F{i} on Store {{ ...F{i + 1} }}" for i in range(100)]
    request = call("{ ...F0 } " + " ".join(chain) + " fragment F100 on Store { status }")
    snap = link([orders], request)
    assert [e.evidence.rule for e in snap.edges] == ["unresolved:graphql-analysis-limit"]
