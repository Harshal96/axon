"""Bind relative ES module calls to source-visible functions and Axios clients."""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from tree_sitter import Node

from axon.v1.facts import AxiosExportFact, FileFacts, JsImportCall
from axon.v1.js_bindings import field, source
from axon.v1.models import EdgeKind, Evidence, GraphEdge, symbol_node_id
from axon.v1.source_languages import JS_TS_SUFFIXES

if TYPE_CHECKING:
    from axon.v1.graph import GraphSnapshot
    from axon.v1.js_axios import AxiosCalls

IMPORT_CALL_RULE = "call:js-import"


def _runtime_exports(statement: Node) -> Iterator[tuple[str, Node]]:
    """Yield exported names and their local expressions, ignoring types and re-exports."""
    if statement.child_by_field_name("source") or any(c.type == "type" for c in statement.children):
        return
    declaration = field(statement, "declaration")
    if any(c.type == "default" for c in statement.children):
        value = field(statement, "value") or declaration
        if value is not None:
            yield "default", value
        return
    if declaration and declaration.type in {
        "function_declaration",
        "generator_function_declaration",
    }:
        yield source(field(declaration, "name")), declaration
        return
    if declaration and any(c.type == "const" for c in declaration.children):
        for declarator in declaration.named_children:
            name = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if name and name.type == "identifier" and value is not None:
                yield source(name), name
        return
    for clause in statement.named_children:
        if clause.type != "export_clause":
            continue
        for specifier in clause.named_children:
            if any(c.type == "type" for c in specifier.children):
                continue
            name = specifier.child_by_field_name("name")
            alias = specifier.child_by_field_name("alias") or name
            if name and alias and alias.type == "identifier":
                yield source(alias), name


def record_exports(
    root: Node, axios: AxiosCalls, facts: FileFacts, local_function: Callable[[Node], str]
) -> None:
    for statement in root.named_children:
        if statement.type != "export_statement":
            continue
        for name, expr in _runtime_exports(statement):
            defaults = axios.export_defaults(expr)
            if defaults is not None:
                facts.axios_exports[name] = AxiosExportFact(expr.start_point.row + 1, *defaults)
            elif qualified := local_function(expr):
                facts.js_function_exports[name] = qualified


def record_imported_call(call: Node, caller: str, axios: AxiosCalls, facts: FileFacts) -> bool:
    fn = field(call, "function")
    if fn is None:
        return False
    candidate = axios.imported_call(call, caller)
    if candidate is None:
        return record_imported_callback(fn, caller, axios, facts)
    module, export, request = candidate
    direct = axios.bindings.imported(fn, esm_only=True)
    facts.js_import_calls.append(
        JsImportCall(module, export, caller, request.line, direct == (module, export), request)
    )
    return True


def record_imported_callback(expr: Node, caller: str, axios: AxiosCalls, facts: FileFacts) -> bool:
    module, export = axios.bindings.imported(expr, esm_only=True)
    if not module.startswith(("./", "../")) or not export or export == "*":
        return False
    facts.js_import_calls.append(
        JsImportCall(module, export, caller, expr.start_point.row + 1, True)
    )
    return True


_TYPESCRIPT_SOURCES = {
    ".js": (".ts", ".tsx"),
    ".jsx": (".tsx",),
    ".mjs": (".mts",),
    ".cjs": (".cts",),
}


def _find_module(
    source: FileFacts, specifier: str, modules: dict[str, FileFacts]
) -> FileFacts | None:
    # Work exclusively with indexed repository paths. Bare packages, tsconfig aliases,
    # directories with package.json entry points and ambiguous extensions need a resolver.
    if specifier.endswith(("/", "/.", "/..")):
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(source.path), specifier))
    stem, suffix = posixpath.splitext(path)
    if source.language == "typescript" and suffix in _TYPESCRIPT_SOURCES:
        # A checked-in JS artifact alongside its TypeScript source may disagree.
        candidates = [path, *(stem + ext for ext in _TYPESCRIPT_SOURCES[suffix])]
    elif suffix in JS_TS_SUFFIXES:
        return modules.get(path)
    else:
        candidates = [path + ext for ext in JS_TS_SUFFIXES]
    matches = [modules[candidate] for candidate in candidates if candidate in modules]
    return matches[0] if len(matches) == 1 else None


def resolve_module_calls(snap: GraphSnapshot, repo_id: str, all_facts: list[FileFacts]) -> None:
    """Resolve from this build's indexed files, including when the caller was cached."""
    from axon.v1.js_axios import apply_axios_defaults

    modules = {f.path: f for f in all_facts if f.language in {"javascript", "typescript"}}
    for importer in modules.values():
        for candidate in importer.js_import_calls:
            target = _find_module(importer, candidate.module, modules)
            if target is None:
                continue
            export = target.axios_exports.get(candidate.export)
            if export and candidate.request:
                defaults = (export.base_url, export.method, export.allow_absolute_urls)
                fact = apply_axios_defaults(candidate.request, defaults)
                fact.detail = f"{fact.call_name} via Axios export {target.path}:{export.line}"
                importer.http_calls.append(fact)
            elif candidate.direct and (symbol := target.js_function_exports.get(candidate.export)):
                caller_id = symbol_node_id(repo_id, importer.path, candidate.caller)
                target_id = symbol_node_id(repo_id, target.path, symbol)
                if caller_id in snap.nodes and target_id in snap.nodes:
                    snap.add_edge(
                        GraphEdge(
                            from_id=caller_id,
                            to_id=target_id,
                            kind=EdgeKind.CALLS,
                            evidence=Evidence(
                                file=importer.path,
                                line=candidate.line,
                                rule=IMPORT_CALL_RULE,
                                detail=f"{candidate.export} from {candidate.module}",
                            ),
                        )
                    )
