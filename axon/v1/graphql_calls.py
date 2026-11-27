"""Bind evidenced executions to root fields in the URI's provider schema."""

from __future__ import annotations

import json
from collections import defaultdict
from copy import copy
from dataclasses import dataclass
from typing import TypedDict
from urllib.parse import urlsplit

from graphql import (
    BooleanValueNode,
    DefinitionNode,
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLInterfaceType,
    GraphQLObjectType,
    GraphQLUnionType,
    InlineFragmentNode,
    NameNode,
    Node,
    OperationDefinitionNode,
    SelectionNode,
    SelectionSetNode,
    VariableDefinitionNode,
    VariableNode,
    Visitor,
    separate_operations,
    validate,
    visit,
)
from graphql.language import SKIP, VisitorAction

from axon.v1.graph import GraphSnapshot
from axon.v1.graphql_inputs import InvocationInput
from axon.v1.graphql_schema import ProviderSchema
from axon.v1.linker import declared_base_targets
from axon.v1.models import (
    Confidence,
    EdgeKind,
    Evidence,
    GraphEdge,
    GraphNode,
    NodeKind,
    graphql_external_node_id,
)
from axon.v1.parse_graphql import document_ast, error_positions, source_position
from axon.v1.servicemap import ServiceMap

_MAX_DEPTH = 48
_MAX_SELECTIONS = 2048
_ParseCache = dict[tuple[str, int, int, str], DocumentNode]


class _Unresolved(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason


def _at(node: Node, message: str) -> str:
    path, line, column = source_position(node)
    return f"{path}:{line}:{column}: {message}"


def _invalid(error: GraphQLError) -> _Unresolved:
    positions = error_positions(error)
    where = ", ".join(f"{path}:{line}:{column}" for path, line, column in positions)
    return _Unresolved("invalid-document", f"{where}: {error.message}" if where else error.message)


class _VariableUses(Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def enter_variable_definition(
        self, node: VariableDefinitionNode, *args: object
    ) -> VisitorAction:
        return SKIP

    def enter_variable(self, node: VariableNode, *args: object) -> None:
        self.names.add(node.name.value)


def _variable_uses(doc: DocumentNode) -> set[str]:
    visitor = _VariableUses()
    visit(doc, visitor)
    return visitor.names


@dataclass
class _Operation:
    operation: OperationDefinitionNode
    fragments: dict[str, FragmentDefinitionNode]
    server_document: DocumentNode


class _Projection:
    """Remove client-only branches; retain AST locations for server validation."""

    def __init__(self, fragments: dict[str, FragmentDefinitionNode]):
        self.fragments = fragments
        self.projected: dict[str, FragmentDefinitionNode | None] = {}
        self.active: set[str] = set()
        self.selections = 0
        self.removed_local = False

    def fragment(self, spread: FragmentSpreadNode, depth: int) -> FragmentDefinitionNode | None:
        name = spread.name.value
        if name in self.projected:
            return self.projected[name]
        if name in self.active:
            raise _Unresolved("invalid-document", _at(spread, f"Fragment cycle through '{name}'."))
        fragment = self.fragments.get(name)
        if fragment is None:
            raise _Unresolved("invalid-document", _at(spread, f"Unknown fragment '{name}'."))
        self.active.add(name)
        projected = copy(fragment)
        projected.selection_set = self.selection_set(fragment.selection_set, depth)
        self.active.remove(name)
        result = projected if projected.selection_set.selections else None
        self.projected[name] = result
        return result

    def selection_set(self, group: SelectionSetNode, depth: int) -> SelectionSetNode:
        self.selections += len(group.selections)
        if depth > _MAX_DEPTH or self.selections > _MAX_SELECTIONS:
            raise _Unresolved("analysis-limit", _at(group, "GraphQL selection limit exceeded."))
        selections: list[SelectionNode] = []
        for node in group.selections:
            if any(d.name.value == "client" for d in node.directives):
                self.removed_local = True
                continue
            if isinstance(node, FragmentSpreadNode):
                if self.fragment(node, depth + 1) is None:
                    continue
            elif isinstance(node, (FieldNode, InlineFragmentNode)) and node.selection_set:
                nested = self.selection_set(node.selection_set, depth + 1)
                if not nested.selections:
                    if isinstance(node, InlineFragmentNode):
                        continue
                    # Apollo's default cache transform adds __typename inside remote
                    # fields. The parent is still fetched when every authored child
                    # is local. Retain the braces' location for server validation.
                    nested.selections = (
                        FieldNode(
                            name=NameNode(value="__typename", loc=nested.loc),
                            arguments=(),
                            directives=(),
                            loc=nested.loc,
                        ),
                    )
                node = copy(node)
                node.selection_set = nested
            selections.append(node)
        return SelectionSetNode(selections=tuple(selections), loc=group.loc)


def _operation(call: InvocationInput, cache: _ParseCache) -> _Operation:
    if not call.documents:
        raise _Unresolved("dynamic-document", "No statically bound execution document.")
    documents: list[DefinitionNode] = []
    fragments: dict[str, FragmentDefinitionNode] = {}
    operations: list[OperationDefinitionNode] = []
    sources = set()
    for source in call.documents:
        key = (source.path, source.line, source.column, source.text)
        if key in sources:
            continue
        sources.add(key)
        if key not in cache:
            cache[key] = document_ast(source)
        documents.extend(cache[key].definitions)
    for definition in documents:
        if isinstance(definition, FragmentDefinitionNode):
            name = definition.name.value
            if name in fragments:
                raise _Unresolved(
                    "invalid-document", _at(definition, f"Duplicate fragment '{name}'.")
                )
            fragments[name] = definition
        elif isinstance(definition, OperationDefinitionNode):
            operations.append(definition)
        else:
            raise _Unresolved("invalid-document", _at(definition, "Non-executable definition."))
    choices = operations
    if call.operation_name:
        choices = [op for op in operations if op.name and op.name.value == call.operation_name]
    if len(choices) != 1:
        raise _Unresolved("ambiguous-operation", "Cannot select one operation for this invocation.")
    operation = choices[0]
    expected = "mutation" if call.kind == "mutate" else call.kind
    if operation.operation.value != expected:
        raise _Unresolved("operation-kind", _at(operation, f"Expected a {expected} operation."))

    return _server_operation(operation, operations, fragments, DocumentNode(definitions=documents))


def _server_operation(
    selected: OperationDefinitionNode,
    operations: list[OperationDefinitionNode],
    fragments: dict[str, FragmentDefinitionNode],
    authored: DocumentNode,
) -> _Operation:
    # Validate the document that would reach the server. Other named operations and
    # unused fragments can invalidate the request even though only one operation runs.
    projection = _Projection(fragments)
    projected_ops = []
    for original in operations:
        projected = copy(original)
        projected.selection_set = projection.selection_set(original.selection_set, 0)
        if original is selected:
            selected = projected
        if projected.selection_set.selections:
            projected_ops.append(projected)
    reachable = {name: frag for name, frag in projection.projected.items() if frag is not None}
    if not projection.removed_local:
        return _Operation(selected, reachable, authored)

    original_docs = separate_operations(authored)
    originally_used = {
        frag.name.value
        for doc in original_docs.values()
        for frag in doc.definitions
        if isinstance(frag, FragmentDefinitionNode)
    }
    unused = [frag for name, frag in fragments.items() if name not in originally_used]
    server = DocumentNode(definitions=(*projected_ops, *reachable.values(), *unused))
    projected_docs = separate_operations(server)
    for op in projected_ops:
        if op.variable_definitions:
            name = op.name.value if op.name else ""
            removed = _variable_uses(original_docs[name]) - _variable_uses(projected_docs[name])
            op.variable_definitions = tuple(
                var for var in op.variable_definitions if var.variable.name.value not in removed
            )
    return _Operation(selected, reachable, server)


class _Selection(TypedDict):
    file: str
    line: int
    column: int
    alias: str
    conditional: bool


def _included(node: SelectionNode) -> tuple[bool, bool]:
    """Return (can run, depends on a variable). Validation checks directive types."""
    conditional = False
    for directive in node.directives:
        name = directive.name.value
        if name in {"skip", "include"}:
            for argument in directive.arguments:
                if argument.name.value != "if":
                    continue
                if isinstance(argument.value, BooleanValueNode):
                    if argument.value.value == (name == "skip"):
                        return False, False
                else:
                    conditional = True
    return True, conditional


def _condition_matches(condition: str, root: str, provider: ProviderSchema | None) -> bool:
    if not provider or condition == root:
        return True
    if not provider.schema:
        raise _Unresolved(
            "unknown-condition", f"Incomplete schema for type condition '{condition}'."
        )
    abstract = provider.schema.get_type(condition)
    concrete = provider.schema.get_type(root)
    return (
        isinstance(abstract, (GraphQLInterfaceType, GraphQLUnionType))
        and isinstance(concrete, GraphQLObjectType)
        and provider.schema.is_sub_type(abstract, concrete)
    )


def _root_fields(
    operation: _Operation, root: str, provider: ProviderSchema | None
) -> dict[str, list[_Selection]]:
    found: dict[str, dict[tuple[str, int, int], _Selection]] = defaultdict(dict)
    pending = [(operation.operation.selection_set, False, 0)]
    visited: set[tuple[str, bool]] = set()
    count = 0
    while pending:
        group, parent_conditional, depth = pending.pop()
        count += len(group.selections)
        if depth > _MAX_DEPTH or count > _MAX_SELECTIONS:
            raise _Unresolved(
                "analysis-limit", _at(group, "GraphQL fragment expansion limit exceeded.")
            )
        for item in group.selections:
            included, conditional = _included(item)
            if not included:
                continue
            conditional |= parent_conditional
            if isinstance(item, FieldNode):
                name = item.name.value
                if name in {"__typename", "__schema", "__type"}:
                    continue
                pos = source_position(item)
                site = found[name].get(pos)
                if site is None:
                    found[name][pos] = _Selection(
                        file=pos[0],
                        line=pos[1],
                        column=pos[2],
                        alias=item.alias.value if item.alias else "",
                        conditional=conditional,
                    )
                elif not conditional:
                    site["conditional"] = False
                continue
            if isinstance(item, FragmentSpreadNode):
                key = (item.name.value, conditional)
                if key in visited:
                    continue
                visited.add(key)
                fragment: InlineFragmentNode | FragmentDefinitionNode = operation.fragments[
                    item.name.value
                ]
            elif isinstance(item, InlineFragmentNode):
                fragment = item
            else:
                continue
            if fragment.type_condition and not _condition_matches(
                fragment.type_condition.name.value, root, provider
            ):
                continue
            pending.append((fragment.selection_set, conditional, depth + 1))
    return {name: [sites[p] for p in sorted(sites)] for name, sites in found.items()}


def _targets(
    uri: str, sm: ServiceMap, repo_id: str, providers: dict[str, ProviderSchema]
) -> list[ProviderSchema]:
    try:
        url = urlsplit(uri)
    except ValueError:
        return []
    if url.scheme not in {"http", "https"} or not url.hostname:
        return []
    clean = url._replace(query="", fragment="").geturl()
    scopes = declared_base_targets(sm, repo_id, clean, clean)
    return [
        providers[service]
        for repo, service in sorted(scopes)
        if repo == repo_id
        and service in providers
        and (url.path or "/") in providers[service].mounts
    ]


def _external(
    snap: GraphSnapshot,
    repo_id: str,
    call: InvocationInput,
    error: _Unresolved,
    props: dict[str, str],
) -> None:
    target = graphql_external_node_id(repo_id, call.caller_id, call.line, call.column, call.uri)
    snap.add_node(
        GraphNode(
            id=target,
            kind=NodeKind.EXTERNAL_API,
            repo_id=repo_id,
            name=f"GraphQL {props.get('operation_name') or call.kind} {call.uri}".strip(),
            file=call.file,
            line_start=call.line,
            props={**props, "reason": error.reason},
        )
    )
    rule = f"unresolved:graphql-{error.reason}"
    snap.add_edge(
        GraphEdge(
            from_id=call.caller_id,
            to_id=target,
            kind=EdgeKind.CALLS_API,
            confidence=Confidence.AMBIGUOUS,
            evidence=Evidence(file=call.file, line=call.line, rule=rule, detail=str(error)),
            props=props,
        )
    )
    snap.unresolved.append(
        f"unresolved outbound call {call.file}:{call.line}:{call.column} {rule}: {error}"
    )
    snap.diagnostics.append(f"{call.file}:{call.line}:{call.column}: {rule}: {error}")


def _link(
    snap: GraphSnapshot,
    call: InvocationInput,
    targets: list[ProviderSchema],
    props: dict[str, str],
    cache: _ParseCache,
) -> None:
    selected = _operation(call, cache)
    operation = selected.operation
    path, line, column = source_position(operation)
    kind = operation.operation.value
    props.update(
        operation_name=operation.name.value if operation.name else "",
        operation_kind=kind,
        operation_location=json.dumps({"file": path, "line": line, "column": column}),
    )
    if not operation.selection_set.selections:
        return
    if len(targets) != 1:
        names = _root_fields(selected, "", None)
        if not names:
            return
        candidates: set[str] = set()
        for provider in targets:
            root = provider.roots.get(kind, "")
            try:
                possible = _root_fields(selected, root, provider)
            except _Unresolved:
                continue
            candidates.update(
                provider.endpoints[coordinate]
                for name in possible
                if (coordinate := f"{root}.{name}") in provider.endpoints
            )
        props["candidates"] = json.dumps(sorted(candidates))
        if targets:
            raise _Unresolved("ambiguous-target", f"Multiple providers match {call.uri}.")
        raise _Unresolved(
            "unknown-target", f"No declared origin and evidenced mount match {call.uri!r}."
        )

    provider = targets[0]
    if provider.schema is not None:
        errors = validate(provider.schema, selected.server_document, max_errors=1)
        if errors:
            raise _invalid(errors[0])
    root = provider.roots.get(kind, "")
    if not root:
        raise _Unresolved("unknown-root", _at(operation, f"No evidenced {kind} root."))
    fields = _root_fields(selected, root, provider)
    for name, sites in fields.items():
        if f"{root}.{name}" not in provider.endpoints:
            site = sites[0]
            raise _Unresolved(
                "unknown-field",
                f"{site['file']}:{site['line']}:{site['column']}: "
                f"No evidenced field '{root}.{name}'.",
            )
    for name, sites in fields.items():
        coordinate = f"{root}.{name}"
        snap.add_edge(
            GraphEdge(
                from_id=call.caller_id,
                to_id=provider.endpoints[coordinate],
                kind=EdgeKind.CALLS_API,
                confidence=Confidence.INFERRED,
                evidence=Evidence(
                    file=call.file,
                    line=call.line,
                    rule="graphql:apollo-field",
                    detail=f"{kind} {props['operation_name']} selects {coordinate}".strip(),
                ),
                props={
                    **props,
                    "coordinate": coordinate,
                    "selections": json.dumps(sites),
                    "conditional": str(all(s["conditional"] for s in sites)).lower(),
                    "network": "possible",
                },
            )
        )


def link_graphql_calls(
    snap: GraphSnapshot,
    repo_id: str,
    sm: ServiceMap,
    providers: dict[str, ProviderSchema],
    calls: list[InvocationInput],
) -> None:
    cache: _ParseCache = {}
    for call in calls:
        if call.cache_only:
            continue
        targets = _targets(call.uri, sm, repo_id, providers)
        client_dependencies = set(call.dependencies) | {d.path for d in call.documents}
        dependencies = client_dependencies.union(*(provider.dependencies for provider in targets))
        props = {
            "protocol": "graphql",
            "via": "apollo-client",
            "column": str(call.column),
            "uri": call.uri,
            "operation_kind": call.kind,
            "operation_name": call.operation_name,
            "graphql_dependencies": json.dumps(sorted(dependencies)),
            "graphql_client_dependencies": json.dumps(sorted(client_dependencies)),
            "target_services": json.dumps([p.service_id for p in targets]),
            "candidates": "[]",
        }
        try:
            _link(snap, call, targets, props, cache)
        except GraphQLError as error:
            _external(snap, repo_id, call, _invalid(error), props)
        except RecursionError:
            _external(
                snap,
                repo_id,
                call,
                _Unresolved("analysis-limit", "GraphQL nesting limit exceeded."),
                props,
            )
        except _Unresolved as error:
            _external(snap, repo_id, call, error, props)
