"""
Parses TypeScript and JavaScript source files using tree-sitter.
Handles: .ts, .tsx, .js, .jsx files.

Extracts:
  - All top-level and exported declarations:
      class, interface, type alias, enum, function, arrow function,
      const/let/var (when the value is a function or React component),
      React functional components (detected by JSX return type or
      capitalized name + arrow function convention)
  - All import statements with their resolved module specifiers
  - All export statements including re-exports (export { X } from './y')
  - JSX element usages as potential CALLS edges (handled by jsx.py)
  - Method call expressions for CALLS edges

Two parsers are used:
  - tree_sitter_typescript for .ts/.tsx files
  - tree_sitter_javascript for .js/.jsx files

The same visitor logic handles both since the grammars are near-identical.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_TS_PARSER = None
_JS_PARSER = None


def _get_parser(language: str):
    """Returns a cached tree-sitter parser for 'typescript' or 'javascript'."""
    global _TS_PARSER, _JS_PARSER
    from tree_sitter import Language, Parser

    if language == "typescript":
        if _TS_PARSER is None:
            import tree_sitter_typescript as tslib

            # tree-sitter-typescript exposes two grammars: typescript and tsx
            _TS_PARSER = {
                "ts": Parser(Language(tslib.language_typescript())),
                "tsx": Parser(Language(tslib.language_tsx())),
            }
        return _TS_PARSER
    else:
        if _JS_PARSER is None:
            import tree_sitter_javascript as jslib

            _JS_PARSER = Parser(Language(jslib.language()))
        return _JS_PARSER


# ── Data classes ───────────────────────────────────────────────────────────────


@dataclass
class TSImport:
    module_specifier: str  # raw string: '@company/ui', './Button', 'react'
    named_imports: list[str]  # ['Button', 'Input'] or [] for default/namespace
    default_import: str | None = None  # 'React' in: import React from 'react'
    namespace_import: str | None = None  # 'styles' in: import * as styles from
    is_type_only: bool = False  # import type { Foo } from '...'
    is_dynamic: bool = False  # await import('...')


@dataclass
class TSExport:
    exported_names: list[str]  # ['Button', 'Input'] or ['default']
    source_module: str | None = None  # set for re-exports: export { X } from './X'
    is_type_only: bool = False
    is_re_export: bool = False


@dataclass
class TSSymbol:
    name: str
    kind: str  # CLASS | INTERFACE | TYPE | ENUM | FUNCTION |
    # ARROW_FUNCTION | REACT_COMPONENT | VARIABLE |
    # METHOD | GETTER | SETTER | CONSTRUCTOR
    file_path: str
    line_start: int
    line_end: int
    is_exported: bool = False
    is_default_export: bool = False
    is_async: bool = False
    is_generic: bool = False  # has <T> type parameters
    extends: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)
    signature: str | None = None
    return_type: str | None = None
    parent_class: str | None = None
    decorators: list[str] = field(default_factory=list)
    # JSX tags used in return value (for CALLS edges to React components)
    jsx_tags_used: list[str] = field(default_factory=list)
    # Direct function/method call sites
    call_sites: list[dict] = field(default_factory=list)
    # Type references (for structural typing blast radius)
    type_references: list[str] = field(default_factory=list)


@dataclass
class ParsedTSFile:
    path: str
    language: str  # "typescript" | "javascript"
    is_tsx: bool = False
    imports: list[TSImport] = field(default_factory=list)
    exports: list[TSExport] = field(default_factory=list)
    symbols: list[TSSymbol] = field(default_factory=list)
    has_parse_errors: bool = False


# ── Tree-sitter node type constants ───────────────────────────────────────────

_CLASS_KINDS = frozenset(
    {
        "class_declaration",
        "abstract_class_declaration",
    }
)
_INTERFACE_KINDS = frozenset(
    {
        "interface_declaration",
    }
)
_TYPE_ALIAS_KINDS = frozenset(
    {
        "type_alias_declaration",
    }
)
_ENUM_KINDS = frozenset(
    {
        "enum_declaration",
    }
)
_FUNCTION_KINDS = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
    }
)
_METHOD_KINDS = frozenset(
    {
        "method_definition",
        "public_field_definition",
    }
)


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _child_of_type(node, *types: str):
    for child in node.children:
        if child.type in types:
            return child
    return None


def _children_of_type(node, *types: str):
    return [c for c in node.children if c.type in types]


def _is_react_component(name: str, node, source: bytes) -> bool:
    """
    Heuristic: a symbol is a React component if:
      1. Its name starts with an uppercase letter, AND
      2. It's a function/arrow function, AND
      3. Its body contains jsx_element or jsx_self_closing_element nodes
         (tree-sitter parses JSX in .tsx/.jsx files)
    """
    if not name or not name[0].isupper():
        return False
    body_text = _node_text(node, source)
    return (
        "jsx_element" in body_text
        or "jsx_self_closing_element" in body_text
        or "</" in body_text
        or "/>" in body_text
    )


def _extract_decorators(node, source: bytes) -> list[str]:
    result = []
    for child in node.children:
        if child.type == "decorator":
            result.append(_node_text(child, source).lstrip("@").split("(")[0])
    return result


def _extract_type_params(node, source: bytes) -> bool:
    """Returns True if the declaration has type parameters <T, ...>"""
    for child in node.children:
        if child.type == "type_parameters":
            return True
    return False


def _extract_heritage(node, source: bytes) -> tuple[list[str], list[str]]:
    """Returns (extends_list, implements_list) for class declarations."""
    extends_list: list[str] = []
    implements_list: list[str] = []
    for child in node.children:
        if child.type == "class_heritage":
            for c2 in child.children:
                if c2.type == "extends_clause":
                    for c3 in c2.children:
                        if c3.type in ("identifier", "member_expression"):
                            extends_list.append(_node_text(c3, source))
                elif c2.type == "implements_clause":
                    for c3 in c2.children:
                        if c3.type in ("type_identifier", "generic_type"):
                            implements_list.append(_node_text(c3, source))
    return extends_list, implements_list


def _extract_call_sites(node, source: bytes) -> list[dict]:
    """Walk the subtree and collect all call_expression nodes."""
    calls = []

    def walk(n):
        if n.type == "call_expression":
            fn_node = n.children[0] if n.children else None
            if fn_node:
                calls.append(
                    {
                        "callee": _node_text(fn_node, source).split("(")[0],
                        "line": n.start_point[0] + 1,
                    }
                )
        for child in n.children:
            walk(child)

    walk(node)
    return calls


def _extract_jsx_tags(node, source: bytes) -> list[str]:
    """Collect all JSX element opening tag names from the node's subtree."""
    tags = []

    def walk(n):
        if n.type in ("jsx_opening_element", "jsx_self_closing_element"):
            tag = _child_of_type(n, "identifier", "member_expression", "jsx_namespace_name")
            if tag:
                name = _node_text(tag, source)
                # Only include PascalCase tags (React components, not HTML)
                if name and name[0].isupper():
                    tags.append(name)
        for child in n.children:
            walk(child)

    walk(node)
    return tags


def _extract_type_references(node, source: bytes) -> list[str]:
    """Collect type_identifier references used in annotations/return types."""
    refs = []

    def walk(n):
        if n.type == "type_identifier":
            name = _node_text(n, source)
            if (
                name
                and name[0].isupper()
                and name
                not in (
                    "string",
                    "number",
                    "boolean",
                    "void",
                    "any",
                    "never",
                    "undefined",
                    "null",
                    "object",
                    "unknown",
                    "Promise",
                    "Array",
                    "Map",
                    "Set",
                    "Record",
                    "Partial",
                    "Required",
                    "Readonly",
                    "Pick",
                    "Omit",
                    "Exclude",
                    "Extract",
                    "NonNullable",
                    "ReturnType",
                    "InstanceType",
                    "JSX",
                )
            ):
                refs.append(name)
        for child in n.children:
            walk(child)

    walk(node)
    return list(dict.fromkeys(refs))  # deduplicate preserving order


# ── Main visitor ───────────────────────────────────────────────────────────────


class _TSVisitor:
    def __init__(self, path: str, source: bytes, is_tsx: bool):
        self.path = path
        self.source = source
        self.is_tsx = is_tsx
        self.symbols: list[TSSymbol] = []
        self.imports: list[TSImport] = []
        self.exports: list[TSExport] = []
        self._class_stack: list[str] = []
        # Track which names are exported at the top level
        self._exported_names: set[str] = set()

    def visit(self, node) -> None:
        """Walk the program node's top-level statements."""
        for child in node.children:
            ntype = child.type

            # ── Import statements ────────────────────────────────────────────
            if ntype == "import_statement":
                self._visit_import(child)

            # ── Export declarations ──────────────────────────────────────────
            elif ntype == "export_statement":
                self._visit_export(child)

            # ── Top-level declarations (possibly wrapped in export) ──────────
            elif ntype in _CLASS_KINDS:
                self._visit_class(child, is_exported=False)
            elif ntype in _INTERFACE_KINDS:
                self._visit_interface(child, is_exported=False)
            elif ntype in _TYPE_ALIAS_KINDS:
                self._visit_type_alias(child, is_exported=False)
            elif ntype in _ENUM_KINDS:
                self._visit_enum(child, is_exported=False)
            elif ntype in _FUNCTION_KINDS:
                self._visit_function(child, is_exported=False)
            elif ntype == "lexical_declaration":
                self._visit_lexical_decl(child, is_exported=False)

    def _visit_import(self, node) -> None:
        spec_node = _child_of_type(node, "string")
        if not spec_node:
            return
        specifier = _node_text(spec_node, self.source).strip("'\"")
        is_type = any(c.type == "type" for c in node.children)
        named: list[str] = []
        default_import: str | None = None
        namespace_import: str | None = None

        import_clause = _child_of_type(node, "import_clause")
        if import_clause:
            for c in import_clause.children:
                if c.type == "identifier":
                    default_import = _node_text(c, self.source)
                elif c.type == "namespace_import":
                    id_node = _child_of_type(c, "identifier")
                    if id_node:
                        namespace_import = _node_text(id_node, self.source)
                elif c.type == "named_imports":
                    for spec in c.children:
                        if spec.type == "import_specifier":
                            name_node = _child_of_type(spec, "identifier")
                            if name_node:
                                named.append(_node_text(name_node, self.source))

        self.imports.append(
            TSImport(
                module_specifier=specifier,
                named_imports=named,
                default_import=default_import,
                namespace_import=namespace_import,
                is_type_only=is_type,
            )
        )

    def _visit_export(self, node) -> None:
        is_type = any(c.type == "type" for c in node.children)

        # export { X, Y } from './module'  — re-export
        named_node = _child_of_type(node, "export_clause")
        source_node = _child_of_type(node, "string")

        if named_node:
            names = []
            for spec in named_node.children:
                if spec.type == "export_specifier":
                    id_node = _child_of_type(spec, "identifier")
                    if id_node:
                        names.append(_node_text(id_node, self.source))
            source_mod = _node_text(source_node, self.source).strip("'\"") if source_node else None
            self.exports.append(
                TSExport(
                    exported_names=names,
                    source_module=source_mod,
                    is_type_only=is_type,
                    is_re_export=bool(source_mod),
                )
            )
            self._exported_names.update(names)
            return

        # export default / export class / export function / export const
        is_default = any(c.type == "default" for c in node.children)
        for child in node.children:
            if child.type in _CLASS_KINDS:
                self._visit_class(child, is_exported=True, is_default=is_default)
            elif child.type in _INTERFACE_KINDS:
                self._visit_interface(child, is_exported=True)
            elif child.type in _TYPE_ALIAS_KINDS:
                self._visit_type_alias(child, is_exported=True)
            elif child.type in _ENUM_KINDS:
                self._visit_enum(child, is_exported=True)
            elif child.type in _FUNCTION_KINDS:
                self._visit_function(child, is_exported=True, is_default=is_default)
            elif child.type == "lexical_declaration":
                self._visit_lexical_decl(child, is_exported=True, is_default=is_default)

        if is_default:
            self.exports.append(
                TSExport(
                    exported_names=["default"],
                    is_re_export=False,
                )
            )
            # export default identifier;
            for child in node.children:
                if child.type == "identifier":
                    self._exported_names.add(_node_text(child, self.source))

    def _visit_class(self, node, is_exported: bool, is_default: bool = False) -> None:
        name_node = _child_of_type(node, "type_identifier", "identifier")
        name = _node_text(name_node, self.source) if name_node else "__anonymous__"
        extends_list, implements_list = _extract_heritage(node, self.source)
        decorators = _extract_decorators(node, self.source)
        calls = _extract_call_sites(node, self.source)
        jsx_tags = _extract_jsx_tags(node, self.source) if self.is_tsx else []
        type_refs = _extract_type_references(node, self.source)
        is_component = _is_react_component(name, node, self.source)

        self.symbols.append(
            TSSymbol(
                name=name,
                kind="REACT_COMPONENT" if is_component else "CLASS",
                file_path=self.path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                is_exported=is_exported,
                is_default_export=is_default,
                is_generic=_extract_type_params(node, self.source),
                extends=extends_list,
                implements=implements_list,
                decorators=decorators,
                call_sites=calls,
                jsx_tags_used=jsx_tags,
                type_references=type_refs,
            )
        )

        # Visit class body for methods
        body = _child_of_type(node, "class_body")
        if body:
            self._class_stack.append(name)
            for member in body.children:
                if member.type in _METHOD_KINDS:
                    self._visit_method(member)
            self._class_stack.pop()

    def _visit_interface(self, node, is_exported: bool) -> None:
        name_node = _child_of_type(node, "type_identifier")
        name = _node_text(name_node, self.source) if name_node else "__interface__"
        extends_list: list[str] = []
        for child in node.children:
            if child.type == "extends_type_clause":
                for c2 in child.children:
                    if c2.type in ("type_identifier", "generic_type"):
                        extends_list.append(_node_text(c2, self.source))
        type_refs = _extract_type_references(node, self.source)
        self.symbols.append(
            TSSymbol(
                name=name,
                kind="INTERFACE",
                file_path=self.path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                is_exported=is_exported,
                is_generic=_extract_type_params(node, self.source),
                extends=extends_list,
                type_references=type_refs,
            )
        )

    def _visit_type_alias(self, node, is_exported: bool) -> None:
        name_node = _child_of_type(node, "type_identifier")
        name = _node_text(name_node, self.source) if name_node else "__type__"
        type_refs = _extract_type_references(node, self.source)
        self.symbols.append(
            TSSymbol(
                name=name,
                kind="TYPE",
                file_path=self.path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                is_exported=is_exported,
                is_generic=_extract_type_params(node, self.source),
                type_references=type_refs,
            )
        )

    def _visit_enum(self, node, is_exported: bool) -> None:
        name_node = _child_of_type(node, "identifier")
        name = _node_text(name_node, self.source) if name_node else "__enum__"
        self.symbols.append(
            TSSymbol(
                name=name,
                kind="ENUM",
                file_path=self.path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                is_exported=is_exported,
            )
        )

    def _visit_function(self, node, is_exported: bool, is_default: bool = False) -> None:
        name_node = _child_of_type(node, "identifier")
        name = _node_text(name_node, self.source) if name_node else "__fn__"
        is_async = any(c.type == "async" for c in node.children)
        calls = _extract_call_sites(node, self.source)
        jsx_tags = _extract_jsx_tags(node, self.source) if self.is_tsx else []
        type_refs = _extract_type_references(node, self.source)
        is_component = _is_react_component(name, node, self.source)
        self.symbols.append(
            TSSymbol(
                name=name,
                kind="REACT_COMPONENT" if is_component else "FUNCTION",
                file_path=self.path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                is_exported=is_exported,
                is_default_export=is_default,
                is_async=is_async,
                is_generic=_extract_type_params(node, self.source),
                call_sites=calls,
                jsx_tags_used=jsx_tags,
                type_references=type_refs,
            )
        )

    def _visit_lexical_decl(self, node, is_exported: bool, is_default: bool = False) -> None:
        """Handles: const/let/var declarations that may contain arrow functions."""
        for declarator in _children_of_type(node, "variable_declarator"):
            name_node = _child_of_type(declarator, "identifier")
            if not name_node:
                continue
            name = _node_text(name_node, self.source)

            # Value: arrow_function or function_expression?
            value = None
            for c in declarator.children:
                if c.type in ("arrow_function", "function_expression", "generator_function"):
                    value = c
                    break

            if value is None:
                # Plain variable (not a function) — still create a VARIABLE symbol
                # if it's exported (likely a constant export)
                if is_exported:
                    self.symbols.append(
                        TSSymbol(
                            name=name,
                            kind="VARIABLE",
                            file_path=self.path,
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            is_exported=is_exported,
                        )
                    )
                continue

            is_async = any(c.type == "async" for c in value.children)
            calls = _extract_call_sites(value, self.source)
            jsx_tags = _extract_jsx_tags(value, self.source) if self.is_tsx else []
            type_refs = _extract_type_references(value, self.source)
            is_component = _is_react_component(name, value, self.source)
            self.symbols.append(
                TSSymbol(
                    name=name,
                    kind="REACT_COMPONENT" if is_component else "ARROW_FUNCTION",
                    file_path=self.path,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    is_exported=is_exported,
                    is_default_export=is_default,
                    is_async=is_async,
                    call_sites=calls,
                    jsx_tags_used=jsx_tags,
                    type_references=type_refs,
                )
            )

    def _visit_method(self, node) -> None:
        name_node = _child_of_type(node, "property_identifier", "private_property_identifier")
        if not name_node:
            return
        name = _node_text(name_node, self.source)
        is_async = any(c.type == "async" for c in node.children)
        calls = _extract_call_sites(node, self.source)
        jsx_tags = _extract_jsx_tags(node, self.source) if self.is_tsx else []

        kind_map = {
            "get": "GETTER",
            "set": "SETTER",
            "constructor": "CONSTRUCTOR",
        }
        kind = "METHOD"
        for c in node.children:
            if c.type in kind_map:
                kind = kind_map[c.type]
                break

        self.symbols.append(
            TSSymbol(
                name=name,
                kind=kind,
                file_path=self.path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                is_exported=False,  # methods are not independently exported
                is_async=is_async,
                parent_class=self._class_stack[-1] if self._class_stack else None,
                call_sites=calls,
                jsx_tags_used=jsx_tags,
            )
        )


# ── Public API ─────────────────────────────────────────────────────────────────


def parse_ts_file(file_path: str) -> ParsedTSFile:
    """
    Parse a single TypeScript or JavaScript file.
    Never raises — logs warnings and returns partial result on error.

    Selects the correct grammar based on file extension:
      .ts  → typescript grammar
      .tsx → tsx grammar (TypeScript + JSX)
      .js  → javascript grammar
      .jsx → javascript grammar (jsx nodes are part of the JS grammar)
    """
    path_obj = Path(file_path)
    ext = path_obj.suffix.lower()
    is_tsx = ext in (".tsx", ".jsx")
    language = "typescript" if ext in (".ts", ".tsx") else "javascript"

    try:
        source = path_obj.read_bytes()
    except OSError as e:
        logger.warning("Cannot read %s: %s", file_path, e)
        return ParsedTSFile(path=file_path, language=language, has_parse_errors=True)

    if language == "typescript":
        parsers = _get_parser("typescript")
        parser = parsers["tsx"] if is_tsx else parsers["ts"]
    else:
        parser = _get_parser("javascript")

    tree = parser.parse(source)
    result = ParsedTSFile(
        path=file_path,
        language=language,
        is_tsx=is_tsx,
        has_parse_errors=tree.root_node.has_error,
    )

    if tree.root_node.has_error:
        logger.warning("Parse errors in %s (continuing with partial result)", file_path)

    visitor = _TSVisitor(file_path, source, is_tsx)
    visitor.visit(tree.root_node)

    # Mark symbols as exported if they appeared in an export clause
    for sym in visitor.symbols:
        if sym.name in visitor._exported_names:
            sym.is_exported = True

    result.imports = visitor.imports
    result.exports = visitor.exports
    result.symbols = visitor.symbols

    logger.debug(
        "Parsed %s: lang=%s symbols=%d imports=%d exports=%d errors=%s",
        file_path,
        language,
        len(result.symbols),
        len(result.imports),
        len(result.exports),
        result.has_parse_errors,
    )
    return result
