"""Go extraction (pattern-based).

Covers func declarations, net/http HandleFunc/Handle, router-style
registrations (GET/POST/... methods with a path and handler), method-
prefixed patterns ("GET /path"), and net/http outbound calls.
"""

from __future__ import annotations

import re

from axon.v1.facts import CallFact, FileFacts, HttpFact, RouteFact, SymFact, join_paths
from axon.v1.source_ranges import delimiter_pairs, mask_source

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

    masked = mask_source(text)
    braces = delimiter_pairs(masked, "{", "}")
    parens = delimiter_pairs(masked, "(", ")")
    funcs: list[tuple[int, int, str]] = []
    declarations: list[tuple[int, int]] = []
    for m in _FUNC_RE.finditer(masked):
        recv, name = m.group(1), m.group(2)
        qualified = f"{recv}.{name}" if recv else name
        opening = m.end() - 1
        end_params = parens.get(opening)
        if end_params is None:
            continue
        cursor = end_params + 1
        while cursor < len(masked):
            if masked[cursor] == "(" and cursor in parens:
                cursor = parens[cursor] + 1
                continue
            if masked[cursor] == "{":
                # Anonymous struct/interface result types are not function bodies.
                before = masked[end_params + 1 : cursor].rstrip()
                if re.search(r"\b(?:struct|interface)$", before) and cursor in braces:
                    cursor = braces[cursor] + 1
                    continue
                break
            cursor += 1
        if cursor not in braces:
            continue
        line = _line_of(text, m.start())
        end = braces[cursor]
        facts.symbols.append(
            SymFact(qualified, name, "METHOD" if recv else "FUNCTION", line, _line_of(text, end))
        )
        funcs.append((cursor, end, qualified))
        declarations.append((m.start(), cursor + 1))

    def enclosing(pos: int) -> str:
        owner = max((f for f in funcs if f[0] < pos < f[1]), key=lambda f: f[0], default=None)
        return owner[2] if owner else ""

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
                catch_all=bool(re.search(r"/\*|\.\*|\.\+", p)),
            )
        )

    seen_routes: set[tuple[str, str, int]] = set()
    for m in _HANDLE_RE.finditer(text):
        if not masked[m.start() : m.start() + 1].strip():
            continue
        key = ("ANY", m.group(1), m.start(1))
        if key not in seen_routes:
            seen_routes.add(key)
            add_route("ANY", m.group(1), m.group(2), m.start())
    for m in _ROUTER_RE.finditer(text):
        if not masked[m.start() : m.start() + 1].strip():
            continue
        verb = m.group(1)
        method = verb.upper() if verb.isupper() else "ANY"
        key = (method, m.group(2), m.start(2))
        if key not in seen_routes:
            seen_routes.add(key)
            add_route(method, m.group(2), m.group(3) or "", m.start())

    for m in _CALL_RE.finditer(masked):
        if any(a <= m.start() < b for a, b in declarations):
            continue
        caller = enclosing(m.start())
        called = f"{m.group(1)}.{m.group(2)}" if m.group(1) else m.group(2)
        if caller and m.group(2) not in ("if", "for", "range", "go", "defer"):
            facts.calls.append(CallFact(caller, called, _line_of(text, m.start())))

    for m in _HTTP_OUT_RE.finditer(text):
        if not masked[m.start() : m.start() + 1].strip():
            continue
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
