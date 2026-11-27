"""Publish source-evidenced GraphQL root fields and attach registered resolver symbols."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from graphql import (
    DefinitionNode,
    DocumentNode,
    ExecutableDefinitionNode,
    FieldDefinitionNode,
    GraphQLError,
    GraphQLSchema,
    NameNode,
    ObjectTypeDefinitionNode,
    ObjectTypeExtensionNode,
    SchemaDefinitionNode,
    SchemaExtensionNode,
    TypeDefinitionNode,
    build_ast_schema,
    print_ast,
    validate_schema,
)
from graphql.validation.validate import validate_sdl

from .facts import FileFacts
from .graph import GraphSnapshot
from .graphql_inputs import DocumentSource, ResolverBinding, ServerInput
from .models import (
    EdgeKind,
    Evidence,
    GraphEdge,
    GraphNode,
    NodeKind,
    graphql_endpoint_node_id,
    service_node_id,
)
from .parse_graphql import document_ast, error_positions, source_position
from .servicemap import ServiceMap


@dataclass
class ProviderSchema:
    service_id: str
    schema: GraphQLSchema | None = None
    roots: dict[str, str] = field(default_factory=dict)
    endpoints: dict[str, str] = field(default_factory=dict)
    mounts: set[str] = field(default_factory=set)
    dependencies: set[str] = field(default_factory=set)


def _identity(doc: DocumentSource) -> tuple[str, int, int]:
    return doc.path, doc.line, doc.column


def _spec_id(repo: str, service: str, path: str) -> str:
    # A shared SDL file can be published independently by several services.
    return f"spec::{repo}::graphql::{service}::{path}"


def _report(snap: GraphSnapshot, service: str, error: GraphQLError) -> None:
    sites = ", ".join(f"{path}:{line}:{column}" for path, line, column in error_positions(error))
    snap.diagnostics.append(f"{sites or service}: graphql:schema ({service}): {error.message}")


def _validation_schema(
    snap: GraphSnapshot, service: str, document: DocumentNode, root_errors: list[GraphQLError]
) -> GraphQLSchema | None:
    errors = root_errors + validate_sdl(document)
    schema = None
    if not errors:
        try:
            schema = build_ast_schema(document, assume_valid_sdl=True)
            errors = validate_schema(schema)
        except (TypeError, GraphQLError) as exc:
            # E.g. a member of a union resolves to a non-object type. Some
            # graphql-core schema constructors raise before validate_schema().
            errors = [GraphQLError(str(exc))]
    for error in errors:
        _report(snap, service, error)
    return None if errors else schema


def _root_fields(
    definitions: list[DefinitionNode],
) -> tuple[dict[str, str], dict[str, list[FieldDefinitionNode]], list[GraphQLError]]:
    """Find unambiguous roots independently of unrelated missing/invalid SDL."""
    types: dict[str, list[TypeDefinitionNode]] = defaultdict(list)
    root_maps: dict[str, list[NameNode]] = defaultdict(list)
    fields: dict[str, list[FieldDefinitionNode]] = defaultdict(list)
    errors: list[GraphQLError] = []
    explicit_schemas = 0
    for definition in definitions:
        if isinstance(definition, TypeDefinitionNode):
            types[definition.name.value].append(definition)
        if isinstance(definition, (SchemaDefinitionNode, SchemaExtensionNode)):
            explicit_schemas += isinstance(definition, SchemaDefinitionNode)
            for op in definition.operation_types:
                root_maps[op.operation.value].append(op.type.name)
        if isinstance(definition, (ObjectTypeDefinitionNode, ObjectTypeExtensionNode)):
            for member in definition.fields:
                fields[definition.name.value].append(member)
    if explicit_schemas > 1:
        return {}, {}, errors
    if not explicit_schemas:
        for kind in ("query", "mutation", "subscription"):
            default = kind.capitalize()
            if default in types:
                default_name = types[default][0].name
                if root_maps[kind]:
                    errors.append(
                        GraphQLError(
                            f"Schema extension redefines the implicit {kind} root {default!r}.",
                            [default_name, *root_maps[kind]],
                        )
                    )
                root_maps[kind].append(default_name)
    roots = {
        kind: names[0].value
        for kind, names in root_maps.items()
        if len(names) == 1
        and len(types[names[0].value]) == 1
        and isinstance(types[names[0].value][0], ObjectTypeDefinitionNode)
    }
    # One object cannot implement more than one root operation type.
    kinds_by_type: dict[str, list[str]] = defaultdict(list)
    for kind, name in roots.items():
        kinds_by_type[name].append(kind)
    for name, kinds in kinds_by_type.items():
        if len(kinds) > 1:
            errors.append(
                GraphQLError(
                    f"Root object {name!r} is used for multiple operations: {', '.join(kinds)}.",
                    [root_maps[kind][0] for kind in kinds],
                )
            )
            for kind in kinds:
                del roots[kind]
    return roots, fields, errors


def _publish_fields(
    snap: GraphSnapshot,
    repo_id: str,
    provider: ProviderSchema,
    fields: dict[str, list[FieldDefinitionNode]],
) -> None:
    service = provider.service_id
    dependencies = json.dumps(sorted(provider.dependencies))
    for kind, root in provider.roots.items():
        members: dict[str, list[FieldDefinitionNode]] = defaultdict(list)
        for member in fields[root]:
            members[member.name.value].append(member)
        for name, declarations in members.items():
            if len(declarations) != 1 or name.startswith("__"):
                continue
            member = declarations[0]
            path, line, column = source_position(member.name)
            _, end_line, _ = source_position(member.type)
            if member.loc:
                end_line = member.loc.source.location_offset.line - 1 + member.loc.end_token.line
            coord = f"{root}.{name}"
            endpoint = graphql_endpoint_node_id(repo_id, service, coord)
            provider.endpoints[coord] = endpoint
            args = ", ".join(print_ast(arg) for arg in member.arguments)
            return_type = print_ast(member.type)
            signature = name + (f"({args})" if args else "") + f": {return_type}"
            props = {
                "protocol": "graphql",
                "schema_id": "default",
                "source": "spec",
                "operation_kind": kind,
                "coordinate": coord,
                "root_type": root,
                "field": name,
                "return_type": return_type,
                "signature": signature,
            }
            if provider.mounts:
                props["mounts"] = json.dumps(sorted(provider.mounts))
            snap.add_node(
                GraphNode(
                    id=endpoint,
                    kind=NodeKind.API_ENDPOINT,
                    name=coord,
                    repo_id=repo_id,
                    service_id=service,
                    file=path,
                    line_start=line,
                    line_end=end_line,
                    props=props,
                )
            )
            evidence = Evidence(file=path, line=line, rule="spec:graphql", detail=signature)
            snap.add_edge(
                GraphEdge(
                    from_id=service_node_id(repo_id, service),
                    to_id=endpoint,
                    kind=EdgeKind.EXPOSES,
                    evidence=evidence,
                )
            )
            snap.add_edge(
                GraphEdge(
                    from_id=_spec_id(repo_id, service, path),
                    to_id=endpoint,
                    kind=EdgeKind.DEFINES,
                    evidence=evidence,
                    props={"column": str(column), "graphql_dependencies": dependencies},
                )
            )


def _bind_resolvers(
    snap: GraphSnapshot, provider: ProviderSchema, servers: list[ServerInput]
) -> None:
    bindings: dict[str, list[tuple[ResolverBinding, ServerInput]]] = defaultdict(list)
    dependencies = json.dumps(sorted(provider.dependencies))
    for server in servers:
        for binding in server.resolvers:
            bindings[binding.coordinate].append((binding, server))
    for coord, endpoint in provider.endpoints.items():
        entries = bindings[coord]
        targets = {binding.symbol_id for binding, _ in entries}
        if len(targets) == 1:
            target = snap.nodes.get(next(iter(targets)))
            if target and target.kind == NodeKind.SYMBOL:
                snap.nodes[endpoint].props["source"] = "both"
                for binding, registration in entries:
                    snap.add_edge(
                        GraphEdge(
                            from_id=target.id,
                            to_id=endpoint,
                            kind=EdgeKind.HANDLES,
                            evidence=Evidence(
                                file=binding.file,
                                line=binding.line,
                                rule="graphql:apollo-resolver",
                                detail=coord,
                            ),
                            props={
                                "registration_file": registration.file,
                                "registration_line": str(registration.line),
                                "graphql_dependencies": dependencies,
                            },
                        )
                    )
                continue
        if len(targets) > 1:
            sites = ", ".join(f"{b.file}:{b.line}" for b, _ in entries)
            reason = f"conflicting registered resolvers at {sites}"
        else:
            reason = "no statically bound resolver"
        server = servers[0]
        snap.diagnostics.append(f"{server.file}:{server.line}: graphql:resolver {coord}: {reason}")


def _assemble(
    snap: GraphSnapshot,
    repo_id: str,
    provider: ProviderSchema,
    documents: list[DocumentSource],
    servers: list[ServerInput],
    complete: bool,
) -> None:
    definitions: list[DefinitionNode] = []
    for doc in documents:
        try:
            document = document_ast(doc)
        except GraphQLError as exc:
            _report(snap, provider.service_id, exc)
            complete = False
            continue
        sdl = [
            node for node in document.definitions if not isinstance(node, ExecutableDefinitionNode)
        ]
        if sdl:
            snap.add_node(
                GraphNode(
                    id=_spec_id(repo_id, provider.service_id, doc.path),
                    kind=NodeKind.API_SPEC,
                    name=doc.path,
                    file=doc.path,
                    repo_id=repo_id,
                    service_id=provider.service_id,
                    props={"protocol": "graphql", "schema_id": "default"},
                )
            )
            definitions.extend(sdl)
    if not definitions:
        return
    provider.roots, fields, root_errors = _root_fields(definitions)
    schema = _validation_schema(
        snap, provider.service_id, DocumentNode(definitions=tuple(definitions)), root_errors
    )
    provider.schema = schema if complete else None
    _publish_fields(snap, repo_id, provider, fields)
    if servers:
        _bind_resolvers(snap, provider, servers)


def build_schemas(
    snap: GraphSnapshot,
    repo_id: str,
    sm: ServiceMap,
    files: list[FileFacts],
    servers: list[ServerInput],
) -> dict[str, ProviderSchema]:
    by_path = {facts.path: facts.graphql_document for facts in files if facts.graphql_document}
    by_service: dict[str, list[ServerInput]] = defaultdict(list)
    for server in servers:
        by_service[server.service_id].append(server)

    providers = {}
    for service in sm.services:
        specs = [
            path
            for path in service.specs
            if PurePosixPath(path).suffix.lower() in {".graphql", ".gql"}
        ]
        registrations = by_service[service.id]
        if not (specs or registrations):
            continue
        provider = providers[service.id] = ProviderSchema(service.id, dependencies=set(specs))
        sources = {_identity(doc): doc for spec in specs if (doc := by_path.get(spec))}
        complete = True
        for spec in specs:
            if not by_path.get(spec):
                complete = False
                snap.diagnostics.append(
                    f"{spec}: graphql:schema ({service.id}): declared SDL unavailable"
                )
        scopes = set()
        for server in registrations:
            provider.mounts.update(server.mounts)
            provider.dependencies.update([server.file, *server.dependencies])
            provider.dependencies.update(doc.path for doc in server.documents)
            provider.dependencies.update(binding.file for binding in server.resolvers)
            complete &= server.schema_complete
            if not server.schema_complete:
                snap.diagnostics.append(
                    f"{server.file}:{server.line}: graphql:schema ({service.id}): "
                    "server schema contains unresolved inputs"
                )
            scopes.add(frozenset(_identity(doc) for doc in server.documents))
            for doc in server.documents:
                sources.setdefault(_identity(doc), doc)
        if len(scopes) > 1 or (
            len(registrations) > 1
            and (not all(scopes) or not all(reg.schema_complete for reg in registrations))
        ):
            sites = ", ".join(f"{reg.file}:{reg.line}" for reg in registrations)
            snap.diagnostics.append(
                f"{sites}: graphql:schema-scope-conflict ({service.id}): "
                "cannot identify one shared schema across server registrations"
            )
            continue
        _assemble(
            snap,
            repo_id,
            provider,
            [sources[key] for key in sorted(sources)],
            registrations,
            complete,
        )
    return providers
