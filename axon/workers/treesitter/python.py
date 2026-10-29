"""
Python-specific AST extraction using tree-sitter-python.

Extracts:
  - Class definitions → SymbolKind.CLASS
  - Function definitions (top-level) → SymbolKind.FUNCTION
  - Method definitions (inside class) → SymbolKind.METHOD
  - Import statements → ParsedImport
  - Function/method calls → ParsedCall
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language as TSLanguage
from tree_sitter import Node, Parser

from axon.models.enums import Language, SymbolKind
from axon.workers.treesitter.base import (
    BaseAstParser,
    ParsedCall,
    ParsedFile,
    ParsedImport,
    ParsedSymbol,
)

logger = logging.getLogger(__name__)

PY_LANGUAGE = TSLanguage(tspython.language())


def _node_text(node: Node, source: bytes) -> str:
    """Extract the text of a tree-sitter node."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _is_public(name: str) -> bool:
    """Python convention: names starting with _ are private."""
    return not name.startswith("_")


def _extract_docstring(body_node: Node, source: bytes) -> str | None:
    """Extract the docstring from the first statement of a function/class body."""
    if body_node is None or body_node.child_count == 0:
        return None
    first = body_node.child(0)
    if first is None:
        return None
    # The first statement should be an expression_statement containing a string
    if first.type == "expression_statement" and first.child_count > 0:
        string_node = first.child(0)
        if string_node and string_node.type == "string":
            raw = _node_text(string_node, source)
            # Strip triple-quotes
            for q in ('"""', "'''", '"', "'"):
                if raw.startswith(q) and raw.endswith(q):
                    return raw[len(q) : -len(q)].strip()
            return raw
    return None


def _extract_signature(node: Node, source: bytes) -> str | None:
    """Extract the parameter list from a function definition."""
    params = node.child_by_field_name("parameters")
    if params is None:
        return None
    sig = _node_text(params, source)
    # Also grab return type if present
    ret = node.child_by_field_name("return_type")
    if ret:
        sig += " -> " + _node_text(ret, source)
    return sig


class PythonParser(BaseAstParser):
    """Parses Python files using tree-sitter-python."""

    def __init__(self) -> None:
        self.parser = Parser(PY_LANGUAGE)

    def parse_file(self, file_path: str, repo_root: str) -> ParsedFile | None:
        """
        Parse a single .py file. file_path is repo-relative.
        Returns None if the file doesn't exist or can't be read.
        """
        abs_path = Path(repo_root) / file_path
        if not abs_path.exists() or not abs_path.is_file():
            return None

        try:
            content = abs_path.read_bytes()
        except OSError as e:
            logger.warning("Could not read %s: %s", file_path, e)
            return None

        sha256 = hashlib.sha256(content).hexdigest()
        line_count = len(content.splitlines())

        tree = self.parser.parse(content)

        parsed = ParsedFile(
            path=file_path,
            language=Language.PYTHON,
            sha256=sha256,
            line_count=line_count,
        )

        # Walk the AST
        self._extract_from_node(
            tree.root_node,
            content,
            parsed,
            file_path,
            parent_class=None,
            current_scope=None,
        )

        return parsed

    def _extract_from_node(
        self,
        node: Node,
        source: bytes,
        parsed: ParsedFile,
        file_path: str,
        parent_class: str | None,
        current_scope: str | None,
    ) -> None:
        """Recursively walk the AST and extract symbols, imports, calls."""

        if node.type == "class_definition":
            self._handle_class(node, source, parsed, file_path, parent_class, current_scope)

        elif node.type == "function_definition":
            self._handle_function(node, source, parsed, file_path, parent_class, current_scope)

        elif node.type in ("import_statement", "import_from_statement"):
            self._handle_import(node, source, parsed)

        elif node.type == "call":
            self._handle_call(node, source, parsed, file_path, current_scope)

        else:
            # Recurse into children
            for child in node.children:
                self._extract_from_node(
                    child,
                    source,
                    parsed,
                    file_path,
                    parent_class,
                    current_scope,
                )

    def _handle_class(
        self,
        node: Node,
        source: bytes,
        parsed: ParsedFile,
        file_path: str,
        parent_class: str | None,
        current_scope: str | None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _node_text(name_node, source)
        qualified = f"{Path(file_path).stem}.{name}"
        symbol_id = f"{file_path}::{name}"

        body = node.child_by_field_name("body")
        docstring = _extract_docstring(body, source) if body else None

        parsed.symbols.append(
            ParsedSymbol(
                symbol_id=symbol_id,
                name=name,
                qualified_name=qualified,
                kind=SymbolKind.CLASS,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                docstring=docstring,
                is_public=_is_public(name),
            )
        )

        # Recurse into class body to find methods
        if body:
            for child in body.children:
                self._extract_from_node(
                    child,
                    source,
                    parsed,
                    file_path,
                    parent_class=name,
                    current_scope=symbol_id,
                )

    def _handle_function(
        self,
        node: Node,
        source: bytes,
        parsed: ParsedFile,
        file_path: str,
        parent_class: str | None,
        current_scope: str | None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _node_text(name_node, source)

        if parent_class:
            kind = SymbolKind.METHOD
            qualified = f"{Path(file_path).stem}.{parent_class}.{name}"
            symbol_id = f"{file_path}::{parent_class}.{name}"
        else:
            kind = SymbolKind.FUNCTION
            qualified = f"{Path(file_path).stem}.{name}"
            symbol_id = f"{file_path}::{name}"

        body = node.child_by_field_name("body")
        docstring = _extract_docstring(body, source) if body else None
        signature = _extract_signature(node, source)

        is_test = name.startswith("test_") or name.startswith("test")

        parsed.symbols.append(
            ParsedSymbol(
                symbol_id=symbol_id,
                name=name,
                qualified_name=qualified,
                kind=kind,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                docstring=docstring,
                signature=signature,
                parent_class=parent_class,
                is_public=_is_public(name),
                is_test=is_test,
            )
        )

        # Recurse into the function body to find calls
        if body:
            for child in body.children:
                self._extract_from_node(
                    child,
                    source,
                    parsed,
                    file_path,
                    parent_class=parent_class,
                    current_scope=symbol_id,
                )

    def _handle_import(
        self,
        node: Node,
        source: bytes,
        parsed: ParsedFile,
    ) -> None:
        line = node.start_point[0] + 1

        if node.type == "import_statement":
            # import foo.bar
            for child in node.children:
                if child.type == "dotted_name":
                    parsed.imports.append(
                        ParsedImport(
                            module_path=_node_text(child, source),
                            line=line,
                        )
                    )
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        parsed.imports.append(
                            ParsedImport(
                                module_path=_node_text(name_node, source),
                                line=line,
                            )
                        )

        elif node.type == "import_from_statement":
            # from foo.bar import Baz, Qux
            module_node = node.child_by_field_name("module_name")
            module_path = _node_text(module_node, source) if module_node else ""

            # Check for relative imports (leading dots)
            is_relative = False
            for child in node.children:
                if child.type == "relative_import":
                    is_relative = True
                    # The module name may be inside the relative_import node
                    for sub in child.children:
                        if sub.type == "dotted_name":
                            module_path = _node_text(sub, source)
                    break
                elif child.type == "import_prefix":
                    is_relative = True

            names: list[str] = []
            for child in node.children:
                if child.type == "dotted_name" and child != module_node:
                    names.append(_node_text(child, source))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        names.append(_node_text(name_node, source))

            parsed.imports.append(
                ParsedImport(
                    module_path=module_path,
                    names=names,
                    is_relative=is_relative,
                    line=line,
                )
            )

    def _handle_call(
        self,
        node: Node,
        source: bytes,
        parsed: ParsedFile,
        file_path: str,
        current_scope: str | None,
    ) -> None:
        """Extract a function/method call."""
        if current_scope is None:
            # Calls at module level — use the file as scope
            current_scope = f"{file_path}::<module>"

        func_node = node.child_by_field_name("function")
        if func_node is None:
            return

        called_name = _node_text(func_node, source)
        line = node.start_point[0] + 1

        parsed.calls.append(
            ParsedCall(
                caller_symbol_id=current_scope,
                called_name=called_name,
                line=line,
            )
        )

        # Recurse into arguments (they may contain nested calls)
        args = node.child_by_field_name("arguments")
        if args:
            for child in args.children:
                self._extract_from_node(
                    child,
                    source,
                    parsed,
                    file_path,
                    parent_class=None,
                    current_scope=current_scope,
                )
