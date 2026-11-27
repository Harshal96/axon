"""Extract source-local expressions needed to bind Apollo registrations and executions."""

from __future__ import annotations

from typing import Any

from graphql import GraphQLError, parse
from tree_sitter import Node

from axon.v1.js_apollo_modules import apollo_inputs as apollo_inputs
from axon.v1.js_bindings import (
    FUNCTION_NODES,
    JSBindings,
    arguments,
    field,
    literal,
    member,
    source,
    unwrap,
)
from axon.v1.js_modules import runtime_exports

_CLIENT_PACKAGES = {"@apollo/client", "@apollo/client/core"}
_LINK_PACKAGES = _CLIENT_PACKAGES | {"@apollo/client/link/http"}
_GQL_IMPORTS = {(package, "gql") for package in _CLIENT_PACKAGES | {"graphql-tag"}}
_GQL_IMPORTS.add(("graphql-tag", "default"))
_MIDDLEWARE = {"@apollo/server/express4", "@as-integrations/express4", "@as-integrations/express5"}


class ApolloExpressions:
    """A small expression graph shared by references to the same source binding.

    Offsets identify expressions within a file. Module specifiers stay relative until
    assembly so content-addressed facts can be replayed under a new path.
    """

    def __init__(self, bindings: JSBindings, names: dict[Node, str], data: bytes):
        self.bindings = bindings
        self.names = names
        self.data = data
        self.nodes: dict[str, dict[str, Any]] = {}
        self._active: set[str] = set()
        self._mutated: set[Node | tuple[str, str]] = set()
        for write in bindings.member_writes:
            # A nested write such as resolvers.Query.order = choose() also
            # invalidates the literal resolver map seen through a local alias.
            base, _ = member(write)
            visited: set[Node] = set()
            while base is not None and base not in visited:
                visited.add(base)
                expr = bindings.stable_expr(base)
                if expr is None:
                    break
                self._mutated.add(expr)
                imported = bindings.imported(expr, esm_only=True)
                if imported[0]:
                    self._mutated.add(imported)
                base, _ = member(expr)
        self.exports: dict[str, str] = {}
        self.servers: list[str] = []
        self.mounts: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def _column(self, node: Node) -> int:
        line_start = node.start_byte - node.start_point.column
        return len(self.data[line_start : node.start_byte].decode()) + 1

    def _template(self, node: Node, tagged: bool, depth: int) -> dict[str, Any]:
        # Preserve columns and line numbers after substitutions. Document parts
        # are bound separately so a fragment retains its own source location.
        if any(child.type == "escape_sequence" for child in node.named_children):
            return {}
        raw = node.text or b""
        pieces: list[str] = []
        parts: list[str] = []
        segments: list[str] = []
        offset = 1
        for child in node.named_children:
            if child.type != "template_substitution":
                continue
            start = child.start_byte - node.start_byte
            end = child.end_byte - node.start_byte
            segment = raw[offset:start].decode()
            segments.append(segment)
            pieces.append(segment)
            pieces.append("".join(c if c in "\r\n" else " " for c in raw[start:end].decode()))
            expr = next((c for c in child.named_children if c.type != "comment"), None)
            parts.append(self.expr(expr, depth + 1))
            offset = end
        tail = raw[offset:-1].decode()
        pieces.append(tail)
        if parts and (not tagged or not _definition_boundaries([*segments, tail])):
            return {}
        return {
            "kind": "template",
            "tagged": tagged,
            "text": "".join(pieces),
            "parts": parts,
            "line": node.start_point.row + 1,
            "column": self._column(node) + 1,
        }

    def _object(self, node: Node, depth: int) -> dict[str, Any]:
        entries = []
        for child in node.named_children:
            if child.type == "comment":
                continue
            key = field(child, "key") or field(child, "name") or child
            name = literal(key) or (
                source(key)
                if key.type in {"property_identifier", "shorthand_property_identifier"}
                else ""
            )
            value = field(child, "value") if child.type == "pair" else child
            if any(c.type in {"get", "set"} for c in child.children):
                value = None
            entries.append(
                [name, self.expr(value, depth + 1) if name else "", child.start_point.row + 1]
            )
        return {"kind": "object", "entries": entries, "line": node.start_point.row + 1}

    def _constructor_kind(self, node: Node) -> str:
        module, name = self.bindings.imported(field(node, "constructor"), esm_only=True)
        if (module, name) == ("@apollo/server", "ApolloServer"):
            return "server"
        if name == "ApolloClient" and module in _CLIENT_PACKAGES:
            return "client"
        if name == "HttpLink" and module in _LINK_PACKAGES:
            return "link"
        return ""

    def expr(self, node: Node | None, depth: int = 0) -> str:
        node = self.bindings.stable_expr(node)
        if node is None or depth >= 48 or node in self._mutated:
            return ""
        key = f"{node.start_byte}:{node.end_byte}"
        if key in self.nodes:
            return key
        if key in self._active:
            return ""
        self._active.add(key)
        value = self._value(node, depth)
        self._active.remove(key)
        if not value:
            return ""
        self.nodes[key] = value
        return key

    def _value(self, node: Node, depth: int) -> dict[str, Any]:
        row = node.start_point.row + 1
        value: dict[str, Any]
        if node.type in FUNCTION_NODES and (qualified := self.names.get(node)):
            value = {"kind": "symbol", "name": qualified, "line": row}
        elif node.type == "object":
            value = self._object(node, depth)
        elif node.type == "array":
            value = {
                "kind": "array",
                "items": [
                    self.expr(c, depth + 1) for c in node.named_children if c.type != "comment"
                ],
            }
        elif node.type == "new_expression" and (kind := self._constructor_kind(node)):
            args = arguments(node)
            value = {
                "kind": kind,
                "line": row,
                "options": self.expr(args[0], depth + 1) if args else "",
            }
        elif node.type == "template_string":
            value = self._template(node, False, depth)
        elif node.type == "call_expression":
            tag = self.bindings.imported(field(node, "function"), esm_only=True)
            template = field(node, "arguments")
            if tag not in _GQL_IMPORTS or not template or template.type != "template_string":
                return {}
            value = self._template(template, True, depth)
        elif (string := literal(node)) is not None:
            value = {
                "kind": "string",
                "value": string,
                "line": row,
                "column": self._column(node) + 1,
            }
        else:
            module, name = self.bindings.imported(node, esm_only=True)
            if (
                not module.startswith(("./", "../"))
                or not name
                or name == "*"
                or (module, name) in self._mutated
            ):
                return {}
            value = {"kind": "import", "module": module, "export": name}
        return value

    def record_exports(self, root: Node, function_exports: dict[str, str]) -> None:
        for statement in root.named_children:
            if statement.type == "export_statement":
                for name, expr in runtime_exports(statement):
                    if name not in function_exports and (key := self.expr(expr)):
                        self.exports[name] = key

    def record_new(self, node: Node) -> None:
        if self._constructor_kind(node) == "server" and (key := self.expr(node)):
            self.servers.append(key)

    def record_call(self, call: Node, caller: str) -> bool:
        receiver, name = member(field(call, "function"))
        if name == "use":
            self._mount(receiver, call)
        if name not in {"query", "mutate"}:
            return False
        client = self.expr(receiver)
        if self.nodes.get(client, {}).get("kind") not in {"client", "import"}:
            return False
        args = arguments(call)
        self.calls.append(
            {
                "client": client,
                "options": self.expr(args[0]) if args else "",
                "caller": caller,
                "line": call.start_point.row + 1,
                "column": self._column(call),
                "kind": "query" if name == "query" else "mutation",
            }
        )
        return True

    def _mount(self, receiver: Node | None, call: Node) -> None:
        app = self.bindings.stable_expr(receiver)
        if not app or app.type != "call_expression":
            return
        if self.bindings.imported(field(app, "function")) != ("express", "default"):
            return
        args = arguments(call)
        path = literal(args[0]) if args else None
        if path is None or not path.startswith("/"):
            return
        for handler in args[1:]:
            handler = unwrap(handler) or handler
            if handler.type != "call_expression":
                continue
            module, name = self.bindings.imported(field(handler, "function"), esm_only=True)
            nested = arguments(handler)
            if module in _MIDDLEWARE and name == "expressMiddleware" and nested:
                if server := self.expr(nested[0]):
                    self.mounts.append({"server": server, "path": path})

    def facts(self) -> dict[str, Any]:
        if not (self.exports or self.servers or self.mounts or self.calls):
            return {}
        return {
            "nodes": self.nodes,
            "exports": self.exports,
            "servers": self.servers,
            "mounts": self.mounts,
            "calls": self.calls,
        }


def _definition_boundaries(segments: list[str]) -> bool:
    """A fragment interpolation must insert a whole definition at document scope.

    Substitutions inside strings, comments or selections cannot be moved out
    into separate source-located documents without changing the request.
    """
    marker = " fragment _AxonInterpolation on Query { __typename } "
    offset = 0
    expected = set()
    for segment in segments[:-1]:
        offset += len(segment)
        expected.add(offset + 1)
        offset += len(marker)
    try:
        document = parse(marker.join(segments))
    except (GraphQLError, RecursionError):
        return False
    return expected <= {d.loc.start for d in document.definitions if d.loc}
