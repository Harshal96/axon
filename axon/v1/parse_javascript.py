"""Extract source-visible functions, Express routes and Axios calls from JS/TS."""

from __future__ import annotations

from pathlib import Path

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from axon.v1.facts import (
    CallFact,
    FileFacts,
    IncludeFact,
    ParseDiagnostic,
    RouteFact,
    SymFact,
    normalize_path,
)
from axon.v1.js_apollo import ApolloExpressions
from axon.v1.js_axios import AxiosCalls
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
from axon.v1.js_modules import record_exports, record_imported_call, record_imported_callback
from axon.v1.source_languages import LANGUAGE_BY_SUFFIX

_GRAMMARS = {
    "javascript": Language(tree_sitter_javascript.language()),
    "typescript": Language(tree_sitter_typescript.language_typescript()),
    "tsx": Language(tree_sitter_typescript.language_tsx()),
}
_CLASSES = {"class_declaration", "class", "abstract_class_declaration"}
_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "all"}
_MODULE = "<module>"


def _location(node: Node) -> str:
    return f"{node.start_point.row + 1}:{node.start_point.column + 1}"


def _bound_name(node: Node) -> str:
    """Get a declaration or property name without interpreting computed keys."""
    parent = node.parent
    key = field(node, "name")
    if parent and parent.type in {
        "variable_declarator",
        "public_field_definition",
        "field_definition",
        "pair",
    }:
        key = field(parent, "name") or field(parent, "property") or field(parent, "key")
    return (
        source(key)
        if key
        and key.type
        in {"identifier", "type_identifier", "property_identifier", "private_property_identifier"}
        else ""
    )


def _params(node: Node) -> list[str]:
    params = field(node, "parameters")
    if not params:
        name = field(node, "parameter")
        return [source(name)] if name else []
    result = []
    for entry in params.named_children:
        if entry.type == "comment":
            continue
        name = field(entry, "pattern") or field(entry, "left") or entry
        # Preserve positions, including those occupied by a destructured parameter.
        result.append(source(name) if name.type == "identifier" else "")
    return result


class _Symbols:
    """Map AST identities to unique symbols before resolving any forward references."""

    def __init__(self, root: Node, facts: FileFacts):
        self.names: dict[Node, str] = {}
        self.calls: list[tuple[Node, str]] = []
        self.constructors: list[Node] = []
        used: set[str] = set()
        stack = [(root, "", _MODULE)]
        while stack:
            node, namespace, caller = stack.pop()
            kind = node.type
            if kind in FUNCTION_NODES or kind in _CLASSES:
                name = _bound_name(node) or f"<callback@{_location(node)}>"
                qualified = f"{namespace}.{name}" if namespace else name
                if qualified in used:
                    qualified += f"@{_location(node)}"
                used.add(qualified)
                self.names[node] = qualified
                symbol_kind = (
                    "CLASS"
                    if kind in _CLASSES
                    else "METHOD"
                    if kind == "method_definition"
                    else "FUNCTION"
                )
                facts.symbols.append(
                    SymFact(
                        qualified,
                        name,
                        symbol_kind,
                        node.start_point.row + 1,
                        node.end_point.row + 1,
                        params=_params(node),
                    )
                )
                namespace = caller = qualified
            elif kind == "object" and (name := _bound_name(node)):
                namespace = f"{namespace}.{name}" if namespace else name
            elif kind == "call_expression":
                self.calls.append((node, caller))
            elif kind == "new_expression":
                self.constructors.append(node)
            elif kind == "import_statement":
                module = literal(field(node, "source"))
                if module:
                    facts.imports.append(module)
            stack.extend((child, namespace, caller) for child in reversed(node.named_children))

    def target(self, expr: Node | None, bindings: JSBindings) -> str:
        """Resolve only a local callable declaration, initializer, or this.method()."""
        for _ in range(16):
            if not (expr := unwrap(expr)):
                break
            if expr.type in FUNCTION_NODES:
                return self.names.get(expr, "")
            if expr.type == "identifier":
                expr = bindings.initializer(expr)
                continue
            receiver, name = member(expr)
            if not receiver or receiver.type != "this":
                break
            # An arrow inherits this; a nested ordinary function introduces its own.
            owner = expr.parent
            while owner and owner.type not in FUNCTION_NODES - {"arrow_function"}:
                owner = owner.parent
            if owner is None or owner.type != "method_definition":
                break
            body = owner.parent
            if body and body.type == "class_body":
                is_static = any(c.type == "static" for c in owner.children)
                hits = [
                    child
                    for child in body.named_children
                    if child.type == "method_definition"
                    and _bound_name(child) == name
                    and (any(c.type == "static" for c in child.children) == is_static)
                    and not any(c.type in {"get", "set"} for c in child.children)
                ]
                if len(hits) == 1:
                    return self.names[hits[0]]
            break
        return ""


def _instance(expr: Node | None, bindings: JSBindings) -> tuple[str, str]:
    for _ in range(16):
        if not (expr := unwrap(expr)):
            break
        if expr.type == "identifier":
            expr = bindings.initializer(expr)
            continue
        if expr.type == "call_expression":
            origin = bindings.imported(field(expr, "function"))
            if origin in {("express", "default"), ("express", "Router")}:
                return origin[1], f"express@{_location(expr)}"
        break
    return "", ""


def _route_chain(receiver: Node | None) -> tuple[Node | None, str | None]:
    """Unwrap app.route('/path').get(handler).post(handler) registrations."""
    for _ in range(16):
        if not receiver or receiver.type != "call_expression":
            break
        parent, name = member(field(receiver, "function"))
        if name == "route":
            args = arguments(receiver)
            return parent, literal(args[0]) if len(args) == 1 else None
        if name not in _VERBS:
            break
        receiver = parent
    return None, None


class _Express:
    def __init__(self, bindings: JSBindings, symbols: _Symbols):
        self.bindings = bindings
        self.symbols = symbols
        self.apps: set[str] = set()
        self.mounts: list[IncludeFact] = []
        self.routes: list[RouteFact] = []
        self.handler_sites: dict[str, Node] = {}

    def record(self, call: Node) -> None:
        receiver, verb = member(field(call, "function"))
        if verb not in _VERBS and verb != "use":
            return
        args = arguments(call)
        if not args:
            return
        base, route_path = _route_chain(receiver)
        kind, router = _instance(base or receiver, self.bindings)
        if not router:
            return
        if kind == "default":
            self.apps.add(router)
        line = call.start_point.row + 1
        if verb == "use":
            prefix = literal(args[0])
            children = args[1:] if prefix is not None else args
            for child in children:
                child_kind, child_id = _instance(child, self.bindings)
                # A dynamic first argument might itself be the mount path.
                if child_kind and (prefix is not None or child == args[0]):
                    self.mounts.append(
                        IncludeFact(prefix or "", "", line, child_id, "express", router)
                    )
            return
        if base is None:
            route_path = literal(args[0])
            args = args[1:]
        if not route_path or not route_path.startswith("/") or not args:
            return
        for handler in args:
            local = self.symbols.target(handler, self.bindings)
            # Give an opaque/imported handler a unique stub. A bare name here
            # would allow the generic linker to select an unrelated local function.
            target = local or f"<handler@{_location(handler)}>"
            if not local:
                self.handler_sites[target] = handler
            operation = (
                local.rsplit(".", 1)[-1]
                if local and handler.type == "identifier" and handler == args[-1]
                else ""
            )
            self.routes.append(
                RouteFact(
                    "ANY" if verb == "all" else verb.upper(),
                    normalize_path(route_path),
                    target,
                    line,
                    "express",
                    operation,
                    router,
                    catch_all=any(c in route_path for c in "*?()+[]"),
                )
            )

    def publish(self, facts: FileFacts, axios: AxiosCalls) -> None:
        # Router registrations describe paths relative to a mount. A known app
        # plus a source-visible mount chain is required to publish a full route.
        reachable = set(self.apps)
        children: dict[str, list[str]] = {}
        for mount in self.mounts:
            children.setdefault(mount.router, []).append(mount.symbol)
        queue = list(reachable)
        while queue:
            for child in children.get(queue.pop(), []):
                if child not in reachable:
                    reachable.add(child)
                    queue.append(child)
        facts.includes.extend(m for m in self.mounts if m.router in reachable)
        facts.routes.extend(r for r in self.routes if r.router in reachable)
        for name in dict.fromkeys(r.handler for r in facts.routes):
            if handler := self.handler_sites.get(name):
                record_imported_callback(handler, name, axios, facts)
        unknown = [r for r in self.routes if r.router not in reachable]
        if unknown:
            facts.diagnostics.append(
                ParseDiagnostic(
                    unknown[0].line,
                    "express:unknown-mount",
                    "Express Router has no static same-file application mount",
                )
            )


def _syntax_error(root: Node) -> ParseDiagnostic:
    node = root
    while not (node.is_error or node.is_missing):
        child = next((c for c in node.children if c.has_error or c.is_missing), None)
        if child is None:
            break
        node = child
    return ParseDiagnostic(
        node.start_point.row + 1, "SyntaxError", "JavaScript/TypeScript syntax could not be parsed"
    )


def parse_javascript(path: str, text: str) -> FileFacts:
    suffix = Path(path).suffix.lower()
    language = LANGUAGE_BY_SUFFIX[suffix]
    facts = FileFacts(path, language)
    grammar = "tsx" if suffix == ".tsx" else language
    data = text.encode()
    root = Parser(_GRAMMARS[grammar]).parse(data).root_node
    if root.has_error:
        facts.diagnostics.append(_syntax_error(root))
        return facts
    axios = AxiosCalls(root)
    symbols = _Symbols(root, facts)
    record_exports(root, axios, facts, lambda expr: symbols.target(expr, axios.bindings))
    apollo = ApolloExpressions(axios.bindings, symbols.names, data)
    apollo.record_exports(root, facts.js_function_exports)
    for constructor in symbols.constructors:
        apollo.record_new(constructor)
    express = _Express(axios.bindings, symbols)
    module_calls = False
    # A chained registration starts at the same byte as its receiver.
    for call, caller in sorted(
        symbols.calls, key=lambda entry: (entry[0].start_byte, entry[0].end_byte)
    ):
        express.record(call)
        if apollo.record_call(call, caller):
            module_calls |= caller == _MODULE
        if fact := axios.extract(call, caller):
            facts.http_calls.append(fact)
        elif local := symbols.target(field(call, "function"), axios.bindings):
            facts.calls.append(CallFact(caller, local, call.start_point.row + 1))
        elif not record_imported_call(call, caller, axios, facts):
            if axios.bindings.unbound(field(call, "function"), "require"):
                args = arguments(call)
                if len(args) == 1 and (module := literal(args[0])):
                    facts.imports.append(module)
            continue
        module_calls |= caller == _MODULE
    if module_calls:
        facts.symbols.append(SymFact(_MODULE, _MODULE, "FUNCTION", 1, root.end_point.row + 1))
    express.publish(facts, axios)
    facts.apollo = apollo.facts()
    facts.imports = list(dict.fromkeys(facts.imports))
    return facts
