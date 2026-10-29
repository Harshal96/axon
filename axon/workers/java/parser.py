"""
Parses a single Java source file using tree-sitter and extracts all symbols:
classes, interfaces, enums, methods, constructors, and fields.

Returns a ParsedJavaFile containing:
  - package declaration
  - all import statements (used by resolver.py to link to Bazel targets)
  - all class/interface/enum declarations with their members
  - all method call expressions (for CALLS edges)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Lazy import: tree-sitter is an optional dependency for the central server.
# It is always available on the indexing workers.
_TS_JAVA = None


def _get_parser():
    global _TS_JAVA
    if _TS_JAVA is None:
        import tree_sitter_java
        from tree_sitter import Language, Parser

        _TS_JAVA = Parser(Language(tree_sitter_java.language()))
    return _TS_JAVA


# ── Data classes ───────────────────────────────────────────────────────────────


@dataclass
class JavaImport:
    qualified_name: str  # "com.company.payment.PaymentGateway"
    is_static: bool = False
    is_wildcard: bool = False  # "import com.company.payment.*"
    alias: str | None = None  # Java doesn't have import aliases, always None


@dataclass
class JavaSymbol:
    """One extracted symbol from a Java source file."""

    name: str  # simple name: "PaymentGateway"
    qualified_name: str  # "com.company.payment.PaymentGateway"
    kind: str  # CLASS | INTERFACE | ENUM | METHOD | CONSTRUCTOR
    # FIELD | ANNOTATION_TYPE
    file_path: str
    line_start: int
    line_end: int
    is_public: bool = True
    is_abstract: bool = False
    is_static: bool = False
    is_final: bool = False
    signature: str | None = None  # for methods: "(String id, Decimal amount) -> ChargeResult"
    return_type: str | None = None  # for methods
    parent_class: str | None = None  # qualified name of enclosing class
    extends: list[str] = field(default_factory=list)  # direct superclass(es)
    implements: list[str] = field(default_factory=list)  # interfaces implemented
    annotations: list[str] = field(default_factory=list)
    cyclomatic_complexity: int = 1
    # Call sites found inside this symbol's body
    call_sites: list[dict] = field(default_factory=list)
    # {callee_simple_name, callee_object, line}


@dataclass
class ParsedJavaFile:
    path: str
    package: str  # "com.company.payment"
    imports: list[JavaImport] = field(default_factory=list)
    symbols: list[JavaSymbol] = field(default_factory=list)
    parse_errors: int = 0


# ── Node type constants (tree-sitter Java grammar node types) ─────────────────

_CLASS_TYPES = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "annotation_type_declaration",
        "record_declaration",
    }
)
_METHOD_TYPES = frozenset(
    {
        "method_declaration",
        "constructor_declaration",
    }
)
_FIELD_TYPES = frozenset(
    {
        "field_declaration",
        "constant_declaration",
    }
)


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _get_modifiers(node, source: bytes) -> set[str]:
    mods = set()
    for child in node.children:
        if child.type == "modifiers":
            for mod in child.children:
                if mod.type in (
                    "public",
                    "private",
                    "protected",
                    "abstract",
                    "static",
                    "final",
                    "synchronized",
                    "native",
                ):
                    mods.add(mod.type)
    return mods


def _extract_name(node, source: bytes) -> str:
    for child in node.children:
        if child.type == "identifier":
            return _node_text(child, source)
    return "__unnamed__"


def _extract_type_name(node, source: bytes) -> str | None:
    """Extract the type name from a type_identifier or scoped_type_identifier node."""
    for child in node.children:
        if child.type in ("type_identifier", "scoped_type_identifier", "generic_type", "void_type"):
            return _node_text(child, source)
    return None


def _count_complexity(method_node, source: bytes) -> int:
    """
    Approximate cyclomatic complexity: 1 + number of branching statements
    (if, else if, while, for, foreach, case, catch, &&, ||, ?).
    """
    count = 1
    branch_types = frozenset(
        {
            "if_statement",
            "while_statement",
            "for_statement",
            "enhanced_for_statement",
            "switch_label",
            "catch_clause",
        }
    )
    branch_operators = frozenset({"&&", "||", "?"})

    def walk(n):
        nonlocal count
        if n.type in branch_types:
            count += 1
        if n.type == "binary_expression":
            op = None
            for c in n.children:
                if c.type in branch_operators:
                    op = c.type
            if op:
                count += 1
        for child in n.children:
            walk(child)

    walk(method_node)
    return count


def _extract_call_sites(method_node, source: bytes) -> list[dict]:
    """
    Extract all method_invocation and object_creation_expression nodes.
    Returns [{callee_simple_name, callee_object, line}].
    """
    calls = []

    def walk(n):
        if n.type == "method_invocation":
            callee_obj: str | None = None
            callee_name: str | None = None
            for child in n.children:
                if child.type == "identifier" and callee_name is None:
                    callee_name = _node_text(child, source)
                elif child.type in ("field_access", "identifier"):
                    callee_obj = _node_text(child, source)
            if callee_name:
                calls.append(
                    {
                        "callee_simple_name": callee_name,
                        "callee_object": callee_obj,
                        "line": n.start_point[0] + 1,
                    }
                )
        for child in n.children:
            walk(child)

    walk(method_node)
    return calls


def _extract_superclass(node, source: bytes) -> list[str]:
    result = []
    for child in node.children:
        if child.type == "superclass":
            for c2 in child.children:
                if c2.type in ("type_identifier", "scoped_type_identifier"):
                    result.append(_node_text(c2, source))
    return result


def _extract_interfaces(node, source: bytes) -> list[str]:
    result = []
    for child in node.children:
        if child.type in ("super_interfaces", "interfaces"):
            for c2 in child.children:
                if c2.type == "type_list":
                    for c3 in c2.children:
                        if c3.type in ("type_identifier", "scoped_type_identifier"):
                            result.append(_node_text(c3, source))
    return result


def _extract_annotations(node, source: bytes) -> list[str]:
    result = []
    if node.parent and node.parent.type == "modifiers":
        for sibling in node.parent.children:
            if sibling.type == "marker_annotation" or sibling.type == "annotation":
                result.append(_node_text(sibling, source))
    for child in node.children:
        if child.type == "modifiers":
            for c2 in child.children:
                if c2.type in ("marker_annotation", "annotation"):
                    result.append(_node_text(c2, source))
    return result


class _JavaFileVisitor:
    """
    Walks a tree-sitter Java AST and extracts all symbols.
    Handles nested classes by maintaining a stack of enclosing class names.
    """

    def __init__(self, path: str, package: str, source: bytes):
        self.path = path
        self.package = package
        self.source = source
        self.symbols: list[JavaSymbol] = []
        self._class_stack: list[str] = []  # qualified names of enclosing classes

    def _qualified(self, simple_name: str) -> str:
        if self._class_stack:
            return f"{self._class_stack[-1]}.{simple_name}"
        return f"{self.package}.{simple_name}" if self.package else simple_name

    def visit(self, node) -> None:
        if node.type in _CLASS_TYPES:
            self._visit_class(node)
        else:
            for child in node.children:
                self.visit(child)

    def _visit_class(self, node) -> None:
        name = _extract_name(node, self.source)
        qualified = self._qualified(name)
        mods = _get_modifiers(node, self.source)
        extends = _extract_superclass(node, self.source)
        implements = _extract_interfaces(node, self.source)
        annotations = _extract_annotations(node, self.source)

        kind_map = {
            "class_declaration": "CLASS",
            "interface_declaration": "INTERFACE",
            "enum_declaration": "ENUM",
            "record_declaration": "CLASS",
            "annotation_type_declaration": "ANNOTATION_TYPE",
        }
        kind = kind_map.get(node.type, "CLASS")

        self.symbols.append(
            JavaSymbol(
                name=name,
                qualified_name=qualified,
                kind=kind,
                file_path=self.path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                is_public="public" in mods,
                is_abstract="abstract" in mods,
                is_static="static" in mods,
                is_final="final" in mods,
                extends=extends,
                implements=implements,
                annotations=annotations,
                parent_class=self._class_stack[-1] if self._class_stack else None,
            )
        )

        self._class_stack.append(qualified)
        # Visit class body for nested classes and methods
        for child in node.children:
            if child.type == "class_body":
                for body_child in child.children:
                    if body_child.type in _CLASS_TYPES:
                        self._visit_class(body_child)
                    elif body_child.type in _METHOD_TYPES:
                        self._visit_method(body_child)
                    elif body_child.type in _FIELD_TYPES:
                        self._visit_field(body_child)
        self._class_stack.pop()

    def _visit_method(self, node) -> None:
        name = _extract_name(node, self.source)
        qualified = self._qualified(name)
        mods = _get_modifiers(node, self.source)
        is_constructor = node.type == "constructor_declaration"

        # Build signature string
        params_text = ""
        for child in node.children:
            if child.type == "formal_parameters":
                params_text = _node_text(child, self.source)
                break

        return_type: str | None = None
        if not is_constructor:
            return_type = _extract_type_name(node, self.source)

        sig = f"{params_text} -> {return_type}" if return_type else params_text
        complexity = _count_complexity(node, self.source)
        calls = _extract_call_sites(node, self.source)
        annotations = _extract_annotations(node, self.source)

        self.symbols.append(
            JavaSymbol(
                name=name,
                qualified_name=qualified,
                kind="CONSTRUCTOR" if is_constructor else "METHOD",
                file_path=self.path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                is_public="public" in mods,
                is_abstract="abstract" in mods,
                is_static="static" in mods,
                is_final="final" in mods,
                signature=sig,
                return_type=return_type,
                parent_class=self._class_stack[-1] if self._class_stack else None,
                annotations=annotations,
                cyclomatic_complexity=complexity,
                call_sites=calls,
            )
        )

    def _visit_field(self, node) -> None:
        mods = _get_modifiers(node, self.source)
        field_type = _extract_type_name(node, self.source)

        for child in node.children:
            if child.type == "variable_declarator":
                for c2 in child.children:
                    if c2.type == "identifier":
                        name = _node_text(c2, self.source)
                        qualified = self._qualified(name)
                        self.symbols.append(
                            JavaSymbol(
                                name=name,
                                qualified_name=qualified,
                                kind="FIELD",
                                file_path=self.path,
                                line_start=node.start_point[0] + 1,
                                line_end=node.end_point[0] + 1,
                                is_public="public" in mods,
                                is_static="static" in mods,
                                is_final="final" in mods,
                                return_type=field_type,
                                parent_class=self._class_stack[-1] if self._class_stack else None,
                            )
                        )


# ── Public API ─────────────────────────────────────────────────────────────────


def parse_java_file(file_path: str) -> ParsedJavaFile:
    """
    Parse a single Java source file and return all extracted symbols.
    Never raises — logs warnings and returns a ParsedJavaFile with parse_errors > 0
    if the file could not be fully parsed.
    """
    source = Path(file_path).read_bytes()

    try:
        parser = _get_parser()
        tree = parser.parse(source)
    except Exception as e:
        logger.warning(f"Failed to parse {file_path}: {e}")
        return ParsedJavaFile(path=file_path, package="", parse_errors=1)

    result = ParsedJavaFile(path=file_path, package="")
    result.parse_errors = int(tree.root_node.has_error)

    # Extract package declaration
    for child in tree.root_node.children:
        if child.type == "package_declaration":
            for c2 in child.children:
                if c2.type == "scoped_identifier" or c2.type == "identifier":
                    result.package = _node_text(c2, source)
                    break
            break

    # Extract imports
    for child in tree.root_node.children:
        if child.type == "import_declaration":
            is_static = any(c.type == "static" for c in child.children)
            name_parts = []
            for c in child.children:
                if c.type in ("identifier", "scoped_identifier"):
                    name_parts.append(_node_text(c, source))
            if name_parts:
                full_name = name_parts[-1]
                is_wildcard = full_name == "*"
                if is_wildcard and len(name_parts) > 1:
                    full_name = ".".join(name_parts[:-1])
                result.imports.append(
                    JavaImport(
                        qualified_name=full_name,
                        is_static=is_static,
                        is_wildcard=is_wildcard,
                    )
                )

    # Extract symbols
    try:
        visitor = _JavaFileVisitor(file_path, result.package, source)
        visitor.visit(tree.root_node)
        result.symbols = visitor.symbols
    except Exception as e:
        logger.warning(f"Error extracting symbols from {file_path}: {e}")
        result.parse_errors += 1

    logger.debug(
        "Parsed %s: package=%s symbols=%d imports=%d errors=%s",
        file_path,
        result.package,
        len(result.symbols),
        len(result.imports),
        result.parse_errors,
    )
    return result
