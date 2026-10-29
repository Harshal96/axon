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
        self.router_prefixes: dict[str, str] = {}  # var name -> prefix
        self.url_prefixes: list[str] = [""]

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
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        q = self._qual(node.name)
        kind = "METHOD" if self.stack else "FUNCTION"
        sig = ast.unparse(node.args) if hasattr(ast, "unparse") else ""
        params = [a.arg for a in node.args.posonlyargs + node.args.args]
        self.facts.symbols.append(
            SymFact(q, node.name, kind, node.lineno, node.end_lineno or node.lineno, sig, params)
        )
        for dec in node.decorator_list:
            self._route_decorator(dec, q, node.name, node.lineno)
        self.stack.append(node.name)
        # Visit only the body: decorator calls describe the function,
        # they are not calls made by it. Visiting them would turn route
        # registrations such as @router.get(...) into outbound HTTP.
        for stmt in node.body:
            self.visit(stmt)
        self.stack.pop()

    # -- FastAPI-style decorators ------------------------------------------
    def _route_decorator(self, dec: ast.expr, qualified: str, fname: str, line: int) -> None:
        if not isinstance(dec, ast.Call):
            return
        dotted = _dotted(dec.func)
        parts = dotted.split(".")
        if len(parts) < 2 or parts[-1] not in HTTP_METHODS:
            return
        owner = parts[-2]
        path = _first_str_arg(dec)
        prefix = self.router_prefixes.get(owner, "")
        operation_id = fname
        for kw in dec.keywords:
            if kw.arg == "operation_id":
                operation_id = _str_const(kw.value) or fname
        self.facts.routes.append(
            RouteFact(
                method=parts[-1].upper(),
                path=join_paths(prefix, path),
                handler=qualified,
                line=line,
                framework="fastapi",
                operation_id=operation_id,
            )
        )

    # -- assignments: APIRouter(prefix=...) ----------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call):
            dotted = _dotted(node.value.func)
            if dotted.rsplit(".", 1)[-1] in ("get_V1Api", "get_v1_api") and node.value.args:
                # get_V1Api(paymentscli, 'PAYMENTS_ENDPOINT'): the receiver
                # is that client package, not the helper itself.
                dotted = _dotted(node.value.args[0]) or dotted
            for t in node.targets:
                if isinstance(t, ast.Name) and dotted:
                    self.facts.bindings.append(
                        BindingFact(t.id, dotted, self._caller(), node.lineno)
                    )
            if dotted.endswith("APIRouter") or dotted.endswith("FastAPI"):
                prefix = ""
                for kw in node.value.keywords:
                    if kw.arg == "prefix":
                        prefix = _str_const(kw.value)
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
                CallFact(caller, dotted, node.lineno, _first_str_arg(node), callable_args)
            )
        self._django_route(node, dotted)
        self._include_router(node, dotted)
        self._http_call(node, dotted, caller)
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
        if not view.args or not isinstance(view.args[0], (ast.List, ast.Tuple)):
            return None
        return _str_const(node.args[0]), list(view.args[0].elts)

    def _include_router(self, node: ast.Call, dotted: str) -> None:
        if not dotted.endswith("include_router"):
            return
        # include_router(router, prefix="/x") transfers prefix when the
        # included router var is known; local-file approximation.
        prefix = ""
        for kw in node.keywords:
            if kw.arg == "prefix":
                prefix = _str_const(kw.value)
        if node.args and isinstance(node.args[0], ast.Name):
            var = node.args[0].id
            base = self.router_prefixes.get(var, "")
            self.router_prefixes[var] = join_paths(prefix, base) if prefix else base
            # Re-prefix routes already recorded for this router is handled at
            # index time by ordering; simple files declare prefix upfront.

    def _django_route(self, node: ast.Call, dotted: str) -> None:
        leaf = dotted.rsplit(".", 1)[-1]
        if leaf not in ("path", "re_path", "url"):
            return
        if len(node.args) < 2:
            return
        pattern = _str_const(node.args[0])
        view = node.args[1]
        if isinstance(view, ast.Call) and _dotted(view.func).endswith("include"):
            # include("app.urls") mounts another module's routes under
            # this pattern; include([...]) is expanded by visit_Call.
            if view.args and isinstance(view.args[0], ast.Constant):
                module = _str_const(view.args[0])
                if module:
                    self.facts.includes.append(
                        IncludeFact(
                            prefix=join_paths(self.url_prefixes[-1], pattern),
                            module=module,
                            line=node.lineno,
                        )
                    )
            return
        handler = _dotted(view)
        if isinstance(view, ast.Call) and isinstance(view.func, ast.Attribute):
            # View.as_view() in source is a Call, not a bare Attribute.
            if view.func.attr == "as_view":
                handler = _dotted(view.func.value)
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
                handler=view_name,
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
            path = _str_const(node.args[0])
            method = _str_const(node.args[1]).upper()
            if path.startswith("/"):
                self.facts.http_calls.append(
                    HttpFact(caller, node.lineno, method, path, "generated-client", dotted)
                )
                return
        url = _first_str_arg(node)
        if root in CLIENT_ROOTS and leaf in HTTP_METHODS:
            method = "" if leaf == "request" else leaf.upper()
            if leaf == "request" and node.args:
                method = _str_const(node.args[0]).upper()
                url = _str_const(node.args[1]) if len(node.args) > 1 else url
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

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.facts.imports.append(a.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.facts.imports.append(node.module)


def parse_python(path: str, text: str) -> FileFacts:
    facts = FileFacts(path=path, language="python")
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
    _Visitor(facts).visit(tree)
    return facts
