import json

import pytest
from graphql import GraphQLError, OperationType, print_ast
from graphql.language.ast import ObjectTypeDefinitionNode, OperationDefinitionNode

from axon.v1 import parse_graphql as parser
from axon.v1.facts import facts_from_dict, facts_to_dict
from axon.v1.graph import GraphSnapshot
from axon.v1.graphql_inputs import (
    DocumentSource,
    ResolverBinding,
    ServerInput,
    graphql_dependency_paths,
)
from axon.v1.graphql_schema import build_schemas
from axon.v1.models import EdgeKind, GraphNode, NodeKind
from axon.v1.parse_graphql import document_ast, error_positions, parse_graphql, source_position
from axon.v1.servicemap import ServiceDecl, ServiceMap


def test_document_cache_restores_mixed_definitions_selections_types_and_locations(monkeypatch):
    text = (
        "type Query { orders(ids: [ID!]!): [Order]! }\r\n"
        "extend type Query { other: String }\r\n"
        "{ ...Page   alias: orders(ids: [1]) { id } }\r\n"
        "fragment Page on Query { other }"
    )
    facts = parse_graphql("db.graphql", text)
    assert not facts.diagnostics
    encoded = json.dumps(facts_to_dict(facts))
    recovered = facts_from_dict(json.loads(encoded)).graphql_document
    assert recovered is not None

    def no_parse(*args, **kwargs):
        pytest.fail("cached document was reparsed")

    monkeypatch.setattr(parser, "parse", no_parse)
    doc = document_ast(recovered)
    assert [node.kind for node in doc.definitions] == [
        "object_type_definition",
        "object_type_extension",
        "operation_definition",
        "fragment_definition",
    ]
    root, _, op, _ = doc.definitions
    assert isinstance(root, ObjectTypeDefinitionNode)
    assert print_ast(root.fields[0]) == "orders(ids: [ID!]!): [Order]!"
    assert isinstance(op, OperationDefinitionNode)
    assert op.operation == OperationType.QUERY
    assert op.name is None
    spread, alias = op.selection_set.selections
    assert source_position(spread) == ("db.graphql", 3, 3)
    assert source_position(alias) == ("db.graphql", 3, 13)


def test_graphql_diagnostics_use_one_based_host_positions():
    facts = parse_graphql("bad.gql", "type Query {\n  : ID }")
    assert [(d.line, d.severity) for d in facts.diagnostics] == [(2, "file-degraded")]
    assert "column 3" in facts.diagnostics[0].message
    source = DocumentSource("resolver.ts", "{\n  health\n}", 70, 30)
    doc = document_ast(source)
    op = doc.definitions[0]
    assert isinstance(op, OperationDefinitionNode)
    assert source_position(op) == ("resolver.ts", 70, 30)
    assert source_position(op.selection_set.selections[0]) == ("resolver.ts", 71, 3)
    with pytest.raises(GraphQLError) as exc:
        document_ast(DocumentSource("resolver.ts", "{ ! }", 70, 30))
    assert error_positions(exc.value) == [("resolver.ts", 70, 32)]


def test_declared_custom_roots_and_extensions_exclude_ordinary_types_and_operations():
    docs = {
        "app/base.gql": (
            "schema { query: Store }\ntype Store { status: String }\n"
            "type Mutation { sideEffect: Int }\nquery Ignored { status }"
        ),
        "app/more.graphql": (
            "extend type Store {\n  products(ids: [ID!]!): [String]!\n}\n"
            "extend schema { subscription: Events }\ntype Events { updated: Boolean }"
        ),
        "web/copy.gql": "type Query { notProvided: Int }",
    }
    sm = ServiceMap(
        services=[
            ServiceDecl(id="orders", specs=["app/base.gql", "app/more.graphql"]),
            ServiceDecl(id="web", roots=["web"]),
        ]
    )
    snap = GraphSnapshot()
    schemas = build_schemas(snap, "acme", sm, [parse_graphql(k, v) for k, v in docs.items()], [])
    assert set(schemas) == {"orders"}
    provider = schemas["orders"]
    assert provider.schema
    assert provider.roots == {"query": "Store", "subscription": "Events"}
    assert set(provider.endpoints) == {"Store.status", "Store.products", "Events.updated"}
    assert not snap.diagnostics
    eid = provider.endpoints["Store.products"]
    node = snap.nodes[eid]
    assert "method" not in node.props and "path" not in node.props
    assert node.props["source"] == "spec"
    assert node.props["signature"] == "products(ids: [ID!]!): [String]!"
    assert node.file == "app/more.graphql" and node.line_start == node.line_end == 2
    [defines] = [e for e in snap.edges if e.to_id == eid and e.kind == EdgeKind.DEFINES]
    assert defines.evidence.rule == "spec:graphql" and defines.evidence.line == 2
    assert graphql_dependency_paths(defines) == {"app/base.gql", "app/more.graphql"}


def test_duplicate_coordinates_report_both_locations_and_keep_healthy_fields():
    files = [
        parse_graphql("a.gql", "type Query { health: String, dupe: String }"),
        parse_graphql("b.graphql", "extend type Query {\n dupe: Int }"),
        parse_graphql("c.gql", "type"),
    ]
    sm = ServiceMap(services=[ServiceDecl(id="svc", specs=[f.path for f in files])])
    snap = GraphSnapshot()
    provider = build_schemas(snap, "r", sm, files, [])["svc"]
    assert provider.schema is None
    assert set(provider.endpoints) == {"Query.health"}
    assert any(
        "a.gql:1:" in d and "b.graphql:2:" in d and "Query.dupe" in d for d in snap.diagnostics
    )


def test_conflicting_provider_registrations_do_not_merge_fields():
    sm = ServiceMap(services=[ServiceDecl(id="api", specs=["separate.graphql"])])
    files = [parse_graphql("separate.graphql", "type Query { other: String }")]
    servers = [
        ServerInput(
            "api", "a.ts", 2, [DocumentSource("a.ts", "type Query { a: Int }")], [], ["/a"], []
        ),
        ServerInput(
            "api", "b.ts", 9, [DocumentSource("b.ts", "type Query { b: Int }")], [], ["/b"], []
        ),
    ]
    snap = GraphSnapshot()
    provider = build_schemas(snap, "r", sm, files, servers)["api"]
    assert provider.mounts == {"/a", "/b"}
    assert not provider.endpoints and provider.schema is None
    assert any(
        "schema-scope-conflict" in d and "a.ts:2" in d and "b.ts:9" in d for d in snap.diagnostics
    )


def test_registration_handler_and_conflicts_bind_only_exact_known_symbols():
    sm = ServiceMap(services=[ServiceDecl(id="api")])
    sdl = DocumentSource(
        "def.ts", "type Query {\n bound: ID\n missing: Int, conflicted: Int }", 40, 11
    )
    bindings = [
        ResolverBinding("Query.bound", "handler-id", "map.ts", 8),
        ResolverBinding("Query.conflicted", "handler-id", "map.ts", 10),
        ResolverBinding("Query.conflicted", "another-id", "map.ts", 11),
    ]
    server = ServerInput("api", "server.ts", 15, [sdl], bindings, ["/graph"], ["map.ts"])
    snap = GraphSnapshot()
    snap.add_node(GraphNode(id="handler-id", kind=NodeKind.SYMBOL, file="impl.ts"))
    provider = build_schemas(snap, "r", sm, [], [server])["api"]
    [handles] = [e for e in snap.edges if e.kind == EdgeKind.HANDLES]
    assert (handles.from_id, handles.to_id) == ("handler-id", provider.endpoints["Query.bound"])
    assert (handles.evidence.file, handles.evidence.line) == ("map.ts", 8)
    assert handles.props["registration_file"] == "server.ts"
    assert graphql_dependency_paths(handles) == {"map.ts", "def.ts", "server.ts"}
    field = snap.nodes[handles.to_id]
    assert (field.file, field.line_start, field.props["source"]) == ("def.ts", 41, "both")
    assert snap.nodes[provider.endpoints["Query.conflicted"]].props["source"] == "spec"
    assert any(
        "conflicting registered resolvers at map.ts:10, map.ts:11" in d for d in snap.diagnostics
    )
    assert any(
        "Query.missing" in d and "no statically bound resolver" in d for d in snap.diagnostics
    )


def test_extension_cannot_replace_an_implicit_root_while_other_operations_remain_available():
    files = [
        parse_graphql("a.gql", "type Query { view: Int }\ntype Mutation { old: ID }"),
        parse_graphql("b.gql", "type Actions { new: ID }\nextend schema { mutation: Actions }"),
    ]
    sm = ServiceMap(services=[ServiceDecl(id="api", specs=[file.path for file in files])])
    snap = GraphSnapshot()
    provider = build_schemas(snap, "r", sm, files, [])["api"]
    assert provider.roots == {"query": "Query"}
    assert provider.schema is None
    assert set(provider.endpoints) == {"Query.view"}
    assert any(
        "a.gql:2:" in d and "b.gql:2:" in d and "implicit mutation" in d for d in snap.diagnostics
    )


def test_one_object_cannot_expose_the_same_field_under_two_operation_kinds():
    facts = parse_graphql(
        "schema.gql", "schema { query: Store\n mutation: Store }\ntype Store { write: Int }"
    )
    sm = ServiceMap(services=[ServiceDecl(id="api", specs=[facts.path])])
    snap = GraphSnapshot()
    provider = build_schemas(snap, "r", sm, [facts], [])["api"]
    assert provider.roots == {} and not provider.endpoints and provider.schema is None
    assert any(
        "schema.gql:1:" in d and "schema.gql:2:" in d and "multiple operations" in d
        for d in snap.diagnostics
    )


def test_shared_sdl_keeps_distinct_provider_spec_evidence_and_conflicts():
    shared = parse_graphql("shared/schema.gql", "type Query { status: ID, health: String }")
    clash = parse_graphql("legacy/clash.gql", "extend type Query { status: Int }")
    sm = ServiceMap(
        services=[
            ServiceDecl(id="api", specs=[shared.path]),
            ServiceDecl(id="legacy", specs=[shared.path, clash.path]),
        ]
    )
    snap = GraphSnapshot()
    providers = build_schemas(snap, "r", sm, [shared, clash], [])
    assert providers["api"].schema is not None
    assert providers["legacy"].schema is None
    assert set(providers["api"].endpoints) == {"Query.status", "Query.health"}
    assert set(providers["legacy"].endpoints) == {"Query.health"}
    evidence = [edge for edge in snap.edges if edge.kind == EdgeKind.DEFINES]
    assert len(evidence) == 3
    assert all(
        snap.nodes[edge.from_id].service_id == snap.nodes[edge.to_id].service_id
        for edge in evidence
    )
    assert len({edge.from_id for edge in evidence}) == 2
    assert all(snap.nodes[edge.from_id].file == shared.path for edge in evidence)
