"""Located SDL and executable GraphQL facts, shared by standalone and host-language parsers."""

from __future__ import annotations

from typing import Any

from graphql import (
    DocumentNode,
    GraphQLError,
    Lexer,
    Location,
    Node,
    OperationType,
    Source,
    SourceLocation,
    TokenKind,
    parse,
)
from graphql.language import ast

from .facts import FileFacts, ParseDiagnostic
from .graphql_inputs import DocumentSource

# graphql-core writes the official AST kind names into Node.to_dict(). Limit
# reconstruction to its known language nodes; cache contents never name imports.
_NODE_TYPES: dict[str, type[Node]] = {
    cls.kind: cls for cls in vars(ast).values() if isinstance(cls, type) and issubclass(cls, Node)
}


def _absolute(source: Source, line: int, column: int) -> tuple[str, int, int]:
    offset = source.location_offset
    return source.name, offset.line + line - 1, column + (offset.column - 1 if line == 1 else 0)


def source_position(node: Node) -> tuple[str, int, int]:
    """One-based host position, including the opening line/column of an embedded literal."""
    if node.loc is None:
        return "", 0, 0
    token = node.loc.start_token
    return _absolute(node.loc.source, token.line, token.column)


def error_positions(error: GraphQLError) -> list[tuple[str, int, int]]:
    if error.nodes:
        return [source_position(node) for node in error.nodes if node.loc]
    if error.source:
        return [_absolute(error.source, pos.line, pos.column) for pos in error.locations or ()]
    return []


def _restore(source: Source, definitions: list[dict[str, Any]]) -> DocumentNode:
    # Locations in the JSON cache are half-open character offsets. Lex once to
    # restore the actual token spans (including columns, comments and newlines)
    # without parsing these definitions or resolving anything against a service.
    lexer = Lexer(source)
    starts, ends = {}, {}
    while (token := lexer.advance()).kind != TokenKind.EOF:
        starts[token.start] = ends[token.end] = token

    def decode(value: Any) -> Any:
        if isinstance(value, list):
            return tuple(decode(item) for item in value)
        if not isinstance(value, dict):
            return value
        cls = _NODE_TYPES[value["kind"]]
        fields = {key: decode(value[key]) for key in cls.keys if key != "loc"}
        if "operation" in fields:
            fields["operation"] = OperationType(fields["operation"])
        span = value["loc"]
        return cls(**fields, loc=Location(starts[span["start"]], ends[span["end"]], source))

    return DocumentNode(definitions=decode(definitions))


def document_ast(document: DocumentSource) -> DocumentNode:
    """Load cached standalone definitions, or parse an embedded document at its host offset."""
    source = Source(document.text, document.path, SourceLocation(document.line, document.column))
    try:
        if document.definitions is not None:
            try:
                return _restore(source, document.definitions)
            except (KeyError, TypeError, ValueError):
                # A best-effort cache hit with a stale or malformed AST.
                pass
        return parse(source)
    except RecursionError as exc:
        raise GraphQLError(
            "Document nesting exceeds parser limits.", source=source, positions=[0]
        ) from exc


def parse_graphql(path: str, text: str) -> FileFacts:
    document = DocumentSource(path=path, text=text)
    facts = FileFacts(path=path, language="graphql", graphql_document=document)
    try:
        root = document_ast(document)
        document.definitions = [node.to_dict(locations=True) for node in root.definitions]
    except GraphQLError as error:
        _, line, column = next(iter(error_positions(error)), (path, 1, 1))
        facts.diagnostics.append(
            ParseDiagnostic(line, "SyntaxError", f"{error.message} (column {column})")
        )
    except RecursionError:
        facts.diagnostics.append(
            ParseDiagnostic(1, "SyntaxError", "Document nesting exceeds parser limits.")
        )
    return facts
