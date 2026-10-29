"""Go extraction (pattern-based).

Covers func declarations, net/http HandleFunc/Handle, router-style
registrations (GET/POST/... methods with a path and handler), method-
prefixed patterns ("GET /path"), and net/http outbound calls.
"""

from __future__ import annotations

import re

from axon.v2.facts import CallFact, FileFacts, HttpFact, RouteFact, SymFact, join_paths

_FUNC_RE = re.compile(r"^func\s+(?:\(\s*\w+\s+\*?(\w+)\s*\)\s*)?(\w+)\s*\(", re.MULTILINE)
_HANDLE_RE = re.compile(r"\b(?:http\.)?Handle(?:Func)?\s*\(\s*\"([^\"]*)\"\s*,\s*([\w.]+)")
_ROUTER_RE = re.compile(
    r"\.\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Handle|HandleFunc|Mount|Route)"
    r"\s*\(\s*\"([^\"]*)\"\s*,\s*([\w.]+)?"
)
_CALL_RE = re.compile(r"(?:(\w+(?:\.\w+)*)\.)?(\w+)\s*\(")
_HTTP_OUT_RE = re.compile(
    r"\bhttp\.(Get|Post|Head)\s*\(\s*\"([^\"]*)\""
    r"|\bhttp\.NewRequest(?:WithContext)?\s*\(\s*\"(\w+)\"\s*,\s*\"([^\"]*)\""
    r"|\.Do\s*\("
)
_IMPORT_RE = re.compile(r'"([\w./-]+)"')
_METHOD_PREFIX_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(.*)$")


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def parse_go(path: str, text: str) -> FileFacts:
    facts = FileFacts(path=path, language="go")
    facts.imports = _IMPORT_RE.findall(text)

    funcs: list[tuple[int, str]] = []
    for m in _FUNC_RE.finditer(text):
        recv, name = m.group(1), m.group(2)
        qualified = f"{recv}.{name}" if recv else name
        line = _line_of(text, m.start())
        facts.symbols.append(SymFact(qualified, name, "METHOD" if recv else "FUNCTION", line, line))
        funcs.append((m.start(), qualified))

    def enclosing(pos: int) -> str:
        cur = ""
        for fpos, q in funcs:
            if fpos <= pos:
                cur = q
        return cur

    def add_route(method: str, raw_path: str, handler: str, pos: int) -> None:
        p = raw_path.strip()
        pm = _METHOD_PREFIX_RE.match(p)
        if pm and method in ("ANY", ""):
            method, p = pm.group(1), pm.group(2)
        h = handler.strip()
        if h.startswith("func") or not h:
            h = enclosing(pos)
        facts.routes.append(
            RouteFact(
                method=method or "ANY",
                path=join_paths("", p),
                handler=h,
                line=_line_of(text, pos),
                framework="go-http",
                operation_id=h.rsplit(".", 1)[-1],
            )
        )

    seen_routes: set[tuple[str, str, int]] = set()
    for m in _HANDLE_RE.finditer(text):
        key = ("ANY", m.group(1), m.start())
        if key not in seen_routes:
            seen_routes.add(key)
            add_route("ANY", m.group(1), m.group(2), m.start())
    for m in _ROUTER_RE.finditer(text):
        verb = m.group(1)
        method = verb.upper() if verb.isupper() else "ANY"
        key = (method, m.group(2), m.start())
        if key not in seen_routes:
            seen_routes.add(key)
            add_route(method, m.group(2), m.group(3) or "", m.start())

    for m in _CALL_RE.finditer(text):
        caller = enclosing(m.start())
        called = f"{m.group(1)}.{m.group(2)}" if m.group(1) else m.group(2)
        if caller and m.group(2) not in ("if", "for", "range", "go", "defer"):
            facts.calls.append(CallFact(caller, called, _line_of(text, m.start())))

    for m in _HTTP_OUT_RE.finditer(text):
        caller = enclosing(m.start())
        if m.group(1):
            method = {"Get": "GET", "Post": "POST", "Head": "HEAD"}[m.group(1)]
            facts.http_calls.append(
                HttpFact(
                    caller,
                    _line_of(text, m.start()),
                    method,
                    m.group(2),
                    "net-http",
                    f"http.{m.group(1)}",
                )
            )
        elif m.group(3):
            facts.http_calls.append(
                HttpFact(
                    caller,
                    _line_of(text, m.start()),
                    m.group(3).upper(),
                    m.group(4),
                    "net-http",
                    "http.NewRequest",
                )
            )
    return facts
