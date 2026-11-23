"""Source-local JavaScript bindings used by framework and HTTP extraction."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from tree_sitter import Node

FUNCTION_NODES = {
    "function_declaration",
    "function_expression",
    "generator_function_declaration",
    "generator_function",
    "arrow_function",
    "method_definition",
}
_BLOCKS = {
    "statement_block",
    "for_statement",
    "for_in_statement",
    "catch_clause",
    "switch_body",
    "class_body",
}
_IDENTIFIERS = {
    "identifier",
    "shorthand_property_identifier",
    "shorthand_property_identifier_pattern",
    "type_identifier",
}


def source(node: Node | None) -> str:
    return (node.text or b"").decode("utf-8", errors="replace") if node else ""


def field(node: Node | None, name: str) -> Node | None:
    return node.child_by_field_name(name) if node else None


def literal(node: Node | None) -> str | None:
    """A simple string literal; leave JS escape sequences to runtime."""
    if (
        node
        and node.type == "string"
        and all(c.type == "string_fragment" for c in node.named_children)
    ):
        return source(node)[1:-1]
    return None


def unwrap(node: Node | None) -> Node | None:
    while node and node.type in {
        "parenthesized_expression",
        "non_null_expression",
        "as_expression",
        "satisfies_expression",
        "type_assertion",
    }:
        children = [c for c in node.named_children if c.type != "comment"]
        if not children:
            return None
        node = children[-1] if node.type == "type_assertion" else children[0]
    return node


def member(node: Node | None) -> tuple[Node | None, str]:
    if node and node.type == "member_expression":
        return field(node, "object"), source(field(node, "property"))
    if node and node.type == "subscript_expression":
        return field(node, "object"), literal(field(node, "index")) or ""
    return None, ""


def arguments(call: Node) -> list[Node]:
    args = field(call, "arguments")
    return [c for c in args.named_children if c.type != "comment"] if args else []


def _pattern_names(pattern: Node | None) -> Iterator[Node]:
    if pattern is None:
        return
    if pattern.type in _IDENTIFIERS:
        yield pattern
    elif pattern.type in {"object_pattern", "array_pattern", "rest_pattern", "formal_parameters"}:
        for child in pattern.named_children:
            yield from _pattern_names(child)
    elif pattern.type in {"required_parameter", "optional_parameter"}:
        yield from _pattern_names(field(pattern, "pattern"))
    elif pattern.type in {"assignment_pattern", "object_assignment_pattern"}:
        yield from _pattern_names(field(pattern, "left"))
    elif pattern.type == "pair_pattern":
        yield from _pattern_names(field(pattern, "value"))


@dataclass
class _Binding:
    value: Node | None = None
    key: str = ""  # a property destructured from the initializer
    module: str = ""
    export: str = ""
    commonjs: bool = False


class JSBindings:
    """Lexical declarations with initializers for source-stable local names."""

    def __init__(self, root: Node):
        self._scopes: dict[Node, dict[str, _Binding]] = {root: {}}
        self._mutated_objects: set[Node] = set()
        self.member_writes: list[Node] = []
        writes: list[Node] = []
        stack = [(root, root, root)]
        while stack:
            node, scope, function = stack.pop()
            kind = node.type
            if kind in FUNCTION_NODES:
                name = field(node, "name")
                if name and kind.endswith("_declaration"):
                    self._scopes[scope][source(name)] = _Binding(value=node)
                scope = function = node
                self._scopes[scope] = {}
                if name and kind != "method_definition":
                    self._scopes[scope][source(name)] = _Binding(value=node)
                self._declare(scope, field(node, "parameters") or field(node, "parameter"))
            elif kind in _BLOCKS:
                scope = node
                self._scopes[scope] = {}
                if kind == "catch_clause":
                    self._declare(scope, field(node, "parameter"))
                elif kind == "for_in_statement" and field(node, "kind"):
                    target = function if source(field(node, "kind")) == "var" else scope
                    self._declare(target, field(node, "left"))
                elif kind == "for_in_statement":
                    writes.extend(_pattern_names(field(node, "left")))
            elif kind in {"class_declaration", "abstract_class_declaration", "class"}:
                name = field(node, "name")
                if name and kind != "class":
                    self._scopes[scope][source(name)] = _Binding(value=node)
                scope = node
                self._scopes[scope] = {source(name): _Binding(value=node)} if name else {}
            elif kind == "import_statement":
                self._import(scope, node)
            elif kind == "variable_declarator":
                parent = node.parent
                target = function if parent and parent.type == "variable_declaration" else scope
                self._declare(
                    target,
                    field(node, "name"),
                    field(node, "value"),
                )
            elif kind in {
                "assignment_expression",
                "augmented_assignment_expression",
                "update_expression",
            }:
                left = field(node, "left") or field(node, "argument")
                receiver, _ = member(left)
                if receiver and left:
                    self.member_writes.append(left)
                else:
                    writes.extend(_pattern_names(left))
            stack.extend((c, scope, function) for c in reversed(node.named_children))
        for name_node in writes:
            owner = self._scope_for(name_node)
            if owner:
                self._scopes[owner][source(name_node)] = _Binding()
        for write in self.member_writes:
            receiver, _ = member(write)
            if not receiver or receiver.type != "identifier":
                continue
            value = self.stable_expr(receiver)
            if value is not None:
                self._mutated_objects.add(value)

    def _declare(self, scope: Node, pattern: Node | None, value: Node | None = None) -> None:
        for name in _pattern_names(pattern):
            binding = _Binding()
            if name == pattern:
                binding.value = value
            elif pattern and pattern.type == "object_pattern":
                parent = name.parent
                if parent == pattern:
                    binding = _Binding(value, source(name))
                elif parent and parent.type == "pair_pattern" and parent.parent == pattern:
                    binding = _Binding(value, source(field(parent, "key")))
            name_text = source(name)
            self._scopes[scope][name_text] = (
                _Binding() if name_text in self._scopes[scope] else binding
            )

    def _import(self, scope: Node, node: Node) -> None:
        clause = next(
            (
                c
                for c in node.named_children
                if c.type in {"import_clause", "import_require_clause"}
            ),
            None,
        )
        if not clause:
            return
        module = literal(field(node, "source") or field(clause, "source")) or ""
        if any(c.type == "type" for c in node.children):
            module = ""
        bindings = self._scopes[scope]
        for child in clause.named_children:
            if child.type == "identifier":
                bindings[source(child)] = _Binding(
                    module=module, export="default", commonjs=clause.type == "import_require_clause"
                )
            elif child.type == "namespace_import":
                bindings[source(child.named_children[-1])] = _Binding(module=module, export="*")
            elif child.type == "named_imports":
                for spec in child.named_children:
                    name = field(spec, "name")
                    if name:
                        local = source(field(spec, "alias") or name)
                        runtime = not any(c.type == "type" for c in spec.children)
                        bindings[local] = _Binding(
                            module=module if runtime else "", export=source(name)
                        )

    def _scope_for(self, identifier: Node) -> Node | None:
        name = source(identifier)
        current: Node | None = identifier
        while current:
            if name in self._scopes.get(current, {}):
                return current
            current = current.parent
        return None

    def _binding(self, identifier: Node | None) -> _Binding | None:
        if not identifier or identifier.type not in _IDENTIFIERS:
            return None
        scope = self._scope_for(identifier)
        return self._scopes[scope][source(identifier)] if scope else None

    def initializer(self, identifier: Node | None) -> Node | None:
        binding = self._binding(identifier)
        return binding.value if binding and not binding.key else None

    def destructured(self, identifier: Node | None) -> tuple[Node | None, str]:
        binding = self._binding(identifier)
        return (binding.value, binding.key) if binding and binding.key else (None, "")

    def stable_expr(self, node: Node | None) -> Node | None:
        for _ in range(16):
            node = unwrap(node)
            if not node or node.type not in _IDENTIFIERS:
                return node
            value = self.initializer(node)
            if value is None:
                return node
            node = value
        return None

    def mutated_object(self, node: Node) -> bool:
        return node in self._mutated_objects

    def unbound(self, node: Node | None, name: str) -> bool:
        return bool(
            node
            and node.type == "identifier"
            and source(node) == name
            and self._binding(node) is None
        )

    def imported(
        self, node: Node | None, depth: int = 0, *, esm_only: bool = False
    ) -> tuple[str, str]:
        if not (node := unwrap(node)) or depth >= 16:
            return "", ""
        if binding := self._binding(node):
            if binding.module:
                if esm_only and binding.commonjs:
                    return "", ""
                return binding.module, binding.export
            module, name = self.imported(binding.value, depth + 1, esm_only=esm_only)
            key = binding.key
        elif (
            not esm_only
            and node.type == "call_expression"
            and self.unbound(field(node, "function"), "require")
        ):
            args = arguments(node)
            return (literal(args[0]) or "", "default") if len(args) == 1 else ("", "")
        else:
            receiver, key = member(node)
            if not receiver:
                return "", ""
            module, name = self.imported(receiver, depth + 1, esm_only=esm_only)
            if not key:
                return "", ""
        if not module:
            return "", ""
        if key:
            if esm_only and name != "*":
                return "", ""
            name = key if name in {"default", "*"} else f"{name}.{key}"
        return module, name

    def origin(self, node: Node | None) -> str:
        module, name = self.imported(node)
        return module if name in {"*", "default"} else f"{module}.{name}" if module else ""
