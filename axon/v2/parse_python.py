"""Python extraction via stdlib ast.

Covers symbols/calls plus FastAPI decorators, Django urls (path/re_path/
url/include + DRF routers best effort), and httpx/requests outbound calls.
"""

from __future__ import annotations

import ast
import threading
import warnings

from axon.v2.facts import (
    MAX_DIAGNOSTICS_PER_FILE,
    BindingFact,
    CallFact,
    FileFacts,
    HttpFact,
    IncludeFact,
    ParseDiagnostic,
    RouteFact,
    RouterRegistrationFact,
    SymFact,
    join_paths,
)

# catch_warnings mutates process-global state and is not thread-safe.
# Serialize just the compile; visiting stays outside the lock. Pool
# workers are separate processes with their own lock.
_PARSE_LOCK = threading.Lock()

_INVALID_ESCAPE = "invalid escape sequence"


def _is_invalid_escape(message: str) -> bool:
    return message.startswith(_INVALID_ESCAPE)


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}
CLIENT_ROOTS = {"httpx", "requests", "aiohttp", "urllib"}


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    cur: ast.expr | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    elif isinstance(cur, ast.Call):
        callee = _dotted(cur.func)
        leaf = callee.rsplit(".", 1)[-1]
        if not callee or not (
            leaf[:1].isupper() or leaf.startswith(("get_", "GET_")) or leaf == "super"
        ):
            return ""
        parts.append(callee)
    else:
        return ""
    return ".".join(reversed(parts))


def _str_const(node: ast.expr | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string -> template with {} holes
        out = ""
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out += v.value
            else:
                out += "{var}"
        return out
    return ""


def _first_str_arg(call: ast.Call) -> str:
    for a in call.args:
        s = _str_const(a)
        if s:
            return s
    for kw in call.keywords:
        if kw.arg in ("url", "path"):
            return _str_const(kw.value)
    return ""


class _Visitor(ast.NodeVisitor):
    def __init__(self, facts: FileFacts) -> None:
        self.facts = facts
        self.stack: list[str] = []  # qualified scope stack
        self.scope_kinds: list[str] = []
        self.router_prefixes: dict[str, str] = {}  # var name -> prefix
        self.url_prefixes: list[str] = [""]
        self.frameworks: dict[str, str] = {}
        self.strings: dict[tuple[str, str], str] = {}
        self.drf_routers: dict[str, list[RouteFact]] = {}
        self.actions: dict[str, tuple[list[str], bool, str]] = {}
        self.patterns: dict[str, list[ast.expr]] = {}

    # -- scopes ------------------------------------------------------------
    def _qual(self, name: str) -> str:
        return ".".join([*self.stack, name]) if self.stack else name

    def _caller(self) -> str:
        return ".".join(self.stack)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        q = self._qual(node.name)
        self.facts.symbols.append(
            SymFact(q, node.name, "CLASS", node.lineno, node.end_lineno or node.lineno)
        )
        self.facts.superclasses[q] = [name for base in node.bases if (name := _dotted(base))]
        self.stack.append(node.name)
        self.scope_kinds.append("CLASS")
        self.generic_visit(node)
        self.scope_kinds.pop()
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        q = self._qual(node.name)
        kind = "METHOD" if self.scope_kinds and self.scope_kinds[-1] == "CLASS" else "FUNCTION"
        sig = ast.unparse(node.args) if hasattr(ast, "unparse") else ""
        params = [a.arg for a in node.args.posonlyargs + node.args.args]
        self.facts.symbols.append(
            SymFact(q, node.name, kind, node.lineno, node.end_lineno or node.lineno, sig, params)
        )
        if node.returns:
            self.facts.returns[q] = _dotted(node.returns) or ast.unparse(node.returns)
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and _dotted(dec.func).rsplit(".", 1)[-1] == "action":
                methods, detail, path = ["GET"], False, node.name
                for kw in dec.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = [self._string(item).upper() for item in kw.value.elts]
                    elif kw.arg == "detail" and isinstance(kw.value, ast.Constant):
                        detail = kw.value.value is True
                    elif kw.arg == "url_path":
                        path = self._string(kw.value) or path
                self.actions[q] = (methods, detail, path)
            route_count = len(self.facts.routes)
            self._route_decorator(dec, q, node.name, node.lineno)
            if len(self.facts.routes) == route_count:
                self.visit(dec)
                decorator = _dotted(dec.func) if isinstance(dec, ast.Call) else _dotted(dec)
                if decorator:
                    self.facts.calls.append(CallFact(q, decorator, dec.lineno))
        self.stack.append(node.name)
        self.scope_kinds.append("FUNCTION")
        # Runtime decorators execute in the enclosing scope. Route decorators
        # are registration metadata and must not become outbound HTTP calls.
        for stmt in node.body:
            self.visit(stmt)
        self.scope_kinds.pop()
        self.stack.pop()

    def visit_Return(self, node: ast.Return) -> None:
        if isinstance(node.value, ast.Name):
            nested = self._qual(node.value.id)
            if any(symbol.qualified == nested for symbol in self.facts.symbols):
                self.facts.calls.append(CallFact(self._caller(), nested, node.lineno))
        self.generic_visit(node)

    # -- FastAPI-style decorators ------------------------------------------
    def _route_decorator(self, dec: ast.expr, qualified: str, fname: str, line: int) -> None:
        if not isinstance(dec, ast.Call):
            return
        dotted = _dotted(dec.func)
        parts = dotted.split(".")
        if len(parts) < 2 or parts[-1] not in HTTP_METHODS | {"route", "api_route"}:
            return
        owner = parts[-2]
        path = (
            self._string(dec.args[0])
            if dec.args
            else next(
                (self._string(kw.value) for kw in dec.keywords if kw.arg in {"path", "rule"}), ""
            )
        )
        prefix = self.router_prefixes.get(owner, "")
        operation_id = fname
        for kw in dec.keywords:
            if kw.arg == "operation_id":
                operation_id = self._string(kw.value) or fname
        methods = [parts[-1].upper()]
        if parts[-1] in {"route", "api_route"}:
            methods = ["GET"]
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [
                        self._string(item).upper() for item in kw.value.elts if self._string(item)
                    ]
        for method in methods:
            self.facts.routes.append(
                RouteFact(
                    method=method,
                    path=join_paths(prefix, path),
                    handler=qualified,
                    line=line,
                    framework=self.frameworks.get(owner, "fastapi"),
                    router=owner,
                    operation_id=operation_id,
                )
            )

    # -- assignments: APIRouter(prefix=...) ----------------------------------
    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            assignment = ast.Assign(targets=[node.target], value=node.value, lineno=node.lineno)
            self.visit_Assign(assignment)

    def _patterns(self, node: ast.expr) -> list[ast.expr] | None:
        if isinstance(node, ast.Name):
            return self.patterns.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self._patterns(node.left), self._patterns(node.right)
            return left + right if left is not None and right is not None else None
        if isinstance(node, (ast.List, ast.Tuple)):
            out: list[ast.expr] = []
            for item in node.elts:
                if isinstance(item, ast.Starred):
                    expanded = self._patterns(item.value)
                    if expanded is None:
                        return None
                    out.extend(expanded)
                else:
                    out.append(item)
            return out
        return None

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name) and isinstance(node.op, ast.Add):
            extra = self._patterns(node.value)
            if extra is not None and node.target.id in self.patterns:
                self.patterns[node.target.id] = self.patterns[node.target.id] + extra
                if node.target.id == "urlpatterns":
                    for item in extra:
                        self.visit(item)
                return
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        patterns = self._patterns(node.value)
        if patterns is not None and all(isinstance(t, ast.Name) for t in node.targets):
            for target in node.targets:
                assert isinstance(target, ast.Name)
                self.patterns[target.id] = patterns
            if any(isinstance(t, ast.Name) and t.id == "urlpatterns" for t in node.targets):
                for item in patterns:
                    self.visit(item)
            return
        for target in node.targets:
            if isinstance(target, ast.Name):
                literal = self._string(node.value)
                if literal:
                    self.strings[(self._caller(), target.id)] = literal
        if isinstance(node.value, ast.Call):
            dotted = _dotted(node.value.func)
            if dotted.rsplit(".", 1)[-1] in {
                "get_V1Api",
                "get_v1_api",
                "get_V2Api",
                "get_v2_api",
                "get_internal_api",
                "get_default_api",
                "get_Api",
                "setup_api_client",
            }:
                package = next(
                    (kw.value for kw in node.value.keywords if kw.arg in {"swagger_cli", "client"}),
                    None,
                )
                if package is None and node.value.args:
                    package = node.value.args[0]
                if package is not None:
                    dotted = _dotted(package) or dotted
            for t in node.targets:
                if isinstance(t, ast.Name) and dotted:
                    self.facts.bindings.append(
                        BindingFact(t.id, dotted, self._caller(), node.lineno)
                    )
                elif isinstance(t, ast.Attribute) and dotted:
                    target_ref = _dotted(t)
                    if target_ref.startswith(("self.", "cls.")):
                        cls = next(
                            (
                                name
                                for name, kind in zip(self.stack, self.scope_kinds)
                                if kind == "CLASS"
                            ),
                            "",
                        )
                        if cls:
                            self.facts.bindings.append(
                                BindingFact(target_ref, dotted, cls, node.lineno)
                            )
            leaf = dotted.rsplit(".", 1)[-1]
            if leaf in {"DefaultRouter", "SimpleRouter", "BulkRouter"}:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.drf_routers[t.id] = []
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if leaf in {"Flask", "Blueprint"}:
                        self.frameworks[t.id] = "flask"
                    elif leaf in {"RouteTableDef", "Application"}:
                        self.frameworks[t.id] = "aiohttp"
            if dotted.endswith("APIRouter") or dotted.endswith("FastAPI"):
                prefix = ""
                for kw in node.value.keywords:
                    if kw.arg == "prefix":
                        prefix = self._string(kw.value)
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.router_prefixes[t.id] = prefix
        self.generic_visit(node)

    # -- calls ---------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        dotted = _dotted(node.func)
        caller = self._caller()
        if dotted:
            callable_args: list[tuple[int, str]] = []
            for i, arg in enumerate(node.args):
                if isinstance(arg, (ast.Attribute, ast.Name)):
                    ref = _dotted(arg)
                    if ref and ref != dotted:
                        callable_args.append((i, ref))
                elif (
                    isinstance(arg, ast.Lambda)
                    and isinstance(arg.body, ast.Call)
                    and isinstance(arg.body.func, ast.Name)
                ):
                    # lambda: fn(...) — an invoked parameter in disguise;
                    # record it as a callable ref to that parameter.
                    callable_args.append((i, arg.body.func.id))
            self.facts.calls.append(
                CallFact(
                    caller,
                    dotted,
                    node.lineno,
                    _first_str_arg(node),
                    callable_args,
                    {
                        **{str(i): self._string(arg) for i, arg in enumerate(node.args)},
                        **{
                            kw.arg: self._string(kw.value)
                            for kw in node.keywords
                            if kw.arg is not None
                        },
                    },
                )
            )
        self._drf_registration(node, dotted)
        self._registered_route(node, dotted)
        self._django_route(node, dotted)
        self._include_router(node, dotted)
        before_http = len(self.facts.http_calls)
        self._http_call(node, dotted, caller)
        if len(self.facts.http_calls) > before_http:
            http = self.facts.http_calls[-1]
            params = next((sym.params for sym in self.facts.symbols if sym.qualified == caller), [])
            request = dotted.rsplit(".", 1)[-1] == "request"
            position = 1 if request else 0
            url_node = next(
                (kw.value for kw in node.keywords if kw.arg == "url"),
                node.args[position] if len(node.args) > position else None,
            )
            if isinstance(url_node, ast.Name) and url_node.id in params and not http.url:
                http.url_param = url_node.id
            method_node = next(
                (kw.value for kw in node.keywords if kw.arg == "method"),
                node.args[0] if request and node.args else None,
            )
            if isinstance(method_node, ast.Name) and method_node.id in params and not http.method:
                http.method_param = method_node.id
        inline = self._inline_include(node, dotted)
        if inline is not None:
            pattern, elts = inline
            self.url_prefixes.append(join_paths(self.url_prefixes[-1], pattern))
            for elt in elts:
                self.visit(elt)
            self.url_prefixes.pop()
            return
        self.generic_visit(node)

    def _inline_include(self, node: ast.Call, dotted: str) -> tuple[str, list[ast.expr]] | None:
        """path("api/", include([...])): routes inside the literal list."""
        if dotted.rsplit(".", 1)[-1] not in ("path", "re_path", "url"):
            return None
        if len(node.args) < 2:
            return None
        view = node.args[1]
        if not (isinstance(view, ast.Call) and _dotted(view.func).endswith("include")):
            return None
        if not view.args:
            return None
        target = view.args[0]
        if (
            isinstance(target, ast.Tuple)
            and target.elts
            and isinstance(target.elts[0], (ast.Name, ast.List, ast.BinOp))
        ):
            target = target.elts[0]
        patterns = self._patterns(target)
        if patterns is None:
            return None
        return self._string(node.args[0]), patterns

    def _include_router(self, node: ast.Call, dotted: str) -> None:
        if not dotted.endswith("include_router"):
            return
        prefix = next((self._string(kw.value) for kw in node.keywords if kw.arg == "prefix"), "")
        if node.args and (target := _dotted(node.args[0])):
            self.facts.includes.append(
                IncludeFact(
                    prefix, target, node.lineno, kind="fastapi", router=dotted.rsplit(".", 1)[0]
                )
            )

    def _drf_registration(self, node: ast.Call, dotted: str) -> None:
        owner, _, method = dotted.rpartition(".")
        if method != "register" or owner not in self.drf_routers or len(node.args) < 2:
            return
        prefix, handler = self._string(node.args[0]), _dotted(node.args[1])
        if not prefix or not handler:
            return
        self.facts.router_registrations.append(
            RouterRegistrationFact(owner, prefix, handler, node.lineno)
        )
        standard = {
            "list": ("GET", ""),
            "create": ("POST", ""),
            "retrieve": ("GET", "{pk}"),
            "update": ("PUT", "{pk}"),
            "partial_update": ("PATCH", "{pk}"),
            "destroy": ("DELETE", "{pk}"),
        }
        for symbol in self.facts.symbols:
            if symbol.qualified.rpartition(".")[0] != handler:
                continue
            if symbol.name in standard:
                verb, suffix = standard[symbol.name]
                self.drf_routers[owner].append(
                    RouteFact(
                        verb,
                        join_paths(prefix, suffix),
                        symbol.qualified,
                        node.lineno,
                        "django",
                        symbol.name,
                    )
                )
            elif symbol.qualified in self.actions:
                methods, detail, action_path = self.actions[symbol.qualified]
                path = join_paths(prefix, join_paths("{pk}" if detail else "", action_path))
                for verb in methods:
                    self.drf_routers[owner].append(
                        RouteFact(verb, path, symbol.qualified, node.lineno, "django", symbol.name)
                    )

    def _registered_route(self, node: ast.Call, dotted: str) -> None:
        leaf = dotted.rsplit(".", 1)[-1]
        if leaf == "add_url_rule" and node.args:
            handler = next((_dotted(kw.value) for kw in node.keywords if kw.arg == "view_func"), "")
            if not handler and len(node.args) > 2:
                handler = _dotted(node.args[2])
            methods = ["GET"]
            for kw in node.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [self._string(item).upper() for item in kw.value.elts]
            for method in methods:
                if handler:
                    self.facts.routes.append(
                        RouteFact(method, self._string(node.args[0]), handler, node.lineno, "flask")
                    )
        elif leaf.startswith("add_") and leaf[4:] in HTTP_METHODS and len(node.args) > 1:
            handler = _dotted(node.args[1])
            if handler:
                self.facts.routes.append(
                    RouteFact(
                        leaf[4:].upper(),
                        self._string(node.args[0]),
                        handler,
                        node.lineno,
                        "aiohttp",
                    )
                )

    def _django_route(self, node: ast.Call, dotted: str) -> None:
        leaf = dotted.rsplit(".", 1)[-1]
        if leaf not in ("path", "re_path", "url"):
            return
        if len(node.args) < 2:
            return
        pattern = self._string(node.args[0])
        view = node.args[1]
        if isinstance(view, ast.Call) and _dotted(view.func).endswith("include"):
            # include("app.urls") mounts another module's routes under
            # this pattern; include([...]) is expanded by visit_Call.
            if view.args:
                target_node = view.args[0]
                if isinstance(target_node, ast.Tuple) and target_node.elts:
                    target_node = target_node.elts[0]
                if self._patterns(target_node) is not None:
                    return
                target = _dotted(target_node)
                if target.endswith(".urls"):
                    self.facts.includes.append(
                        IncludeFact(
                            join_paths(self.url_prefixes[-1], pattern),
                            target[:-5],
                            node.lineno,
                            kind="drf",
                        )
                    )
                if target.endswith(".urls") and target[:-5] in self.drf_routers:
                    for route in self.drf_routers[target[:-5]]:
                        self.facts.routes.append(
                            RouteFact(
                                route.method,
                                join_paths(join_paths(self.url_prefixes[-1], pattern), route.path),
                                route.handler,
                                node.lineno,
                                "django",
                                route.operation_id,
                            )
                        )
                    return
                module = self._string(target_node) or _dotted(target_node)
                head, sep, tail = module.partition(".")
                imported = self.facts.import_map.get(head, head)
                module = imported + (sep + tail if sep else "")
                symbol = ""
                # Imported symbols may name a list rather than a URL module.
                # The indexer only uses this split when the module exists.
                if not isinstance(target_node, ast.Constant) and not module.endswith(".urls"):
                    module, _, symbol = module.rpartition(".")
                if module:
                    self.facts.includes.append(
                        IncludeFact(
                            prefix=join_paths(self.url_prefixes[-1], pattern),
                            module=module,
                            line=node.lineno,
                            symbol=symbol,
                        )
                    )
            return
        while (
            isinstance(view, ast.Call) and not _dotted(view.func).endswith("as_view") and view.args
        ):
            view = view.args[0]
        handler = _dotted(view)
        if isinstance(view, ast.Call) and isinstance(view.func, ast.Attribute):
            # View.as_view() in source is a Call, not a bare Attribute.
            if view.func.attr == "as_view":
                handler = _dotted(view.func.value)
                if view.args and isinstance(view.args[0], ast.Dict):
                    for key, value in zip(view.args[0].keys, view.args[0].values):
                        method, action = self._string(key), self._string(value)
                        if method and action:
                            self.facts.routes.append(
                                RouteFact(
                                    method.upper(),
                                    join_paths(self.url_prefixes[-1], pattern),
                                    f"{handler}.{action}",
                                    node.lineno,
                                    "django",
                                    action,
                                )
                            )
                    return
        elif isinstance(view, ast.Attribute) and view.attr == "as_view":
            handler = _dotted(view.value)
        if not handler:
            return
        view_name = handler.rsplit(".", 1)[-1]
        # Django views answer any method; record common verbs as ANY->GET
        # plus note. Keep one endpoint per pattern with method ANY mapped
        # to GET is lossy, so store method ANY as-is.
        self.facts.routes.append(
            RouteFact(
                method="ANY",
                path=join_paths(self.url_prefixes[-1], pattern),
                handler=handler,
                line=node.lineno,
                framework="django",
                operation_id=view_name,
            )
        )

    def _http_call(self, node: ast.Call, dotted: str, caller: str) -> None:
        parts = dotted.split(".")
        if not parts or not caller:
            return
        root = parts[0]
        leaf = parts[-1]
        # Swagger-codegen style: self.api_client.call_api("/path", "GET", ...)
        # inside a generated API class method. Emit the operation directly
        # from the method so the handler closure can reach it.
        if leaf == "call_api" and len(node.args) >= 2:
            path = self._string(node.args[0])
            method = self._string(node.args[1]).upper()
            if path.startswith("/"):
                self.facts.http_calls.append(
                    HttpFact(caller, node.lineno, method, path, "generated-client", caller)
                )
                return
        url = next(
            (self._string(arg) for arg in node.args if self._string(arg)), ""
        ) or _first_str_arg(node)
        if root in CLIENT_ROOTS and leaf in HTTP_METHODS:
            method = "" if leaf == "request" else leaf.upper()
            for kw in node.keywords:
                if kw.arg == "url":
                    url = self._string(kw.value)
            if leaf == "request":
                method = self._string(node.args[0]).upper() if node.args else ""
                if len(node.args) > 1:
                    url = self._string(node.args[1])
                for kw in node.keywords:
                    if kw.arg == "method":
                        method = self._string(kw.value).upper()
            self.facts.http_calls.append(HttpFact(caller, node.lineno, method, url, root, dotted))
            return
        # Instance clients: client.get/post(...), self.http.get(...)
        if leaf in HTTP_METHODS and len(parts) >= 2 and url.startswith(("http", "/")):
            self.facts.http_calls.append(
                HttpFact(caller, node.lineno, leaf.upper(), url, "http-client", dotted)
            )
            return
        # Generated-client style: <something>Client.<method>(...) or
        # module-level imported client objects are resolved at index time
        # via axon.yaml client packages; record candidate when the object
        # name looks like a client.
        if len(parts) >= 2 and "client" in parts[-2].lower():
            self.facts.http_calls.append(
                HttpFact(caller, node.lineno, "", url, "generated-client", dotted)
            )

    def _string(self, node: ast.expr | None) -> str:
        if isinstance(node, ast.Name):
            return self.strings.get((self._caller(), node.id), self.strings.get(("", node.id), ""))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self._string(node.left), self._string(node.right)
            return left + right if left and right else ""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "format":
                template = self._string(node.func.value)
                if template:
                    args = [self._string(arg) or "{var}" for arg in node.args]
                    kwargs = {
                        kw.arg: self._string(kw.value) or "{var}"
                        for kw in node.keywords
                        if kw.arg is not None
                    }
                    try:
                        return template.format(*args, **kwargs)
                    except (ValueError, KeyError, IndexError):
                        return ""
        return _str_const(node)

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.facts.imports.append(a.name)
            self.facts.import_map[a.asname or a.name.split(".")[0]] = (
                a.name if a.asname else a.name.split(".")[0]
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            components = self.facts.package.split(".") if self.facts.package else []
            module = ".".join(
                components[: len(components) - node.level + 1] + ([module] if module else [])
            )
        if module:
            self.facts.imports.append(module)
        for alias in node.names:
            self.facts.import_map[alias.asname or alias.name] = ".".join(
                filter(None, [module, alias.name])
            )


def parse_python(path: str, text: str) -> FileFacts:
    facts = FileFacts(
        path=path, language="python", package=".".join(path.replace("\\", "/").split("/")[:-1])
    )
    try:
        with _PARSE_LOCK, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        facts.diagnostics.append(
            ParseDiagnostic(
                line=exc.lineno or 0,
                category="SyntaxError",
                message=exc.msg or str(exc),
            )
        )
        return facts
    for w in caught:
        if _is_invalid_escape(str(w.message)):
            if len(facts.diagnostics) < MAX_DIAGNOSTICS_PER_FILE:
                facts.diagnostics.append(
                    ParseDiagnostic(
                        line=w.lineno or 0,
                        category=w.category.__name__,
                        message=str(w.message),
                    )
                )
            else:
                facts.diagnostics_truncated += 1
        else:
            warnings.warn_explicit(w.message, w.category, w.filename, w.lineno)
    visitor = _Visitor(facts)
    visitor.visit(tree)
    standard_actions = {
        "list": ("GET", ""),
        "create": ("POST", ""),
        "retrieve": ("GET", "{pk}"),
        "update": ("PUT", "{pk}"),
        "partial_update": ("PATCH", "{pk}"),
        "destroy": ("DELETE", "{pk}"),
    }
    for symbol in facts.symbols:
        if symbol.kind != "METHOD":
            continue
        viewset = symbol.qualified.rpartition(".")[0]
        methods: list[str] = []
        route_path = ""
        if symbol.qualified in visitor.actions:
            methods, detail, action_path = visitor.actions[symbol.qualified]
            route_path = join_paths("{pk}" if detail else "", action_path)
        elif symbol.name in standard_actions:
            method, suffix = standard_actions[symbol.name]
            methods, route_path = [method], join_paths("", suffix)
        for method in methods:
            facts.view_actions.setdefault(viewset, []).append(
                RouteFact(
                    method, route_path, symbol.qualified, symbol.line_start, "django", symbol.name
                )
            )
    for name, patterns in visitor.patterns.items():
        if name == "urlpatterns":
            continue
        start = len(facts.routes)
        for pattern in patterns:
            visitor.visit(pattern)
        facts.pattern_routes[name] = facts.routes[start:]
        del facts.routes[start:]
    return facts
