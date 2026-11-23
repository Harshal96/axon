"""Go extraction (pattern-based).

Covers func declarations, net/http HandleFunc/Handle, router-style
registrations (GET/POST/... methods with a path and handler), method-
prefixed patterns ("GET /path"), and net/http outbound calls.
"""

from __future__ import annotations

import json
import re

from axon.v1.facts import CallFact, FileFacts, HttpFact, RouteFact, SymFact, join_paths
from axon.v1.source_ranges import delimiter_pairs, mask_comments, mask_source, split_expression

_FUNC_RE = re.compile(r"^func\s+(?:\(\s*\w+\s+\*?(\w+)\s*\)\s*)?(\w+)\s*\(", re.MULTILINE)
_HANDLE_RE = re.compile(r"\b(?:http\.)?Handle(?:Func)?\s*\(\s*\"([^\"]*)\"\s*,\s*([\w.]+)")
_ROUTER_RE = re.compile(
    r"\.\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Handle|HandleFunc|Mount|Route)"
    r"\s*\(\s*\"([^\"]*)\"\s*,\s*([\w.]+)?"
)
_CALL_RE = re.compile(r"(?:(\w+(?:\.\w+)*)\.)?(\w+)\s*\(")
_HTTP_OUT_RE = re.compile(r"(?<![\w.])(\w+)\.(Get|Post|Head|NewRequest(?:WithContext)?)\s*\(")
_METHOD_CONSTANT_RE = re.compile(
    r"(\w+)\.Method(Get|Head|Post|Put|Patch|Delete|Connect|Options|Trace)"
)
_IMPORT_SPEC_RE = re.compile(r'(?:(\w+|\.)\s+)?["`]([^"`\n]+)["`]')
_METHOD_PREFIX_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(.*)$")


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _string_literal(expr: str) -> str | None:
    if re.fullmatch(r"`[^`]*`", expr):
        return expr[1:-1].replace("\r", "")
    if re.fullmatch(r'"(?:\\.|[^"\\\n])*"', expr):
        try:
            value: str = json.loads(expr)
            return value
        except ValueError:
            # Go-specific escapes that JSON cannot decode stay unknown.
            pass
    return None


def _request_method(expr: str, package: str) -> str:
    method = _string_literal(expr)
    if method is not None:
        return method.upper() or "GET"
    const = _METHOD_CONSTANT_RE.fullmatch(expr)
    return const.group(2).upper() if const and const.group(1) == package else ""


def parse_go(path: str, text: str) -> FileFacts:
    facts = FileFacts(path=path, language="go")
    masked = mask_source(text)
    code = mask_comments(text)
    braces = delimiter_pairs(masked, "{", "}")
    parens = delimiter_pairs(masked, "(", ")")
    http_packages: set[str] = set()
    for declaration in re.finditer(r"\bimport\b", masked):
        start = declaration.end()
        while start < len(code) and code[start].isspace():
            start += 1
        if start in parens:
            specs = _IMPORT_SPEC_RE.finditer(code, start + 1, parens[start])
        else:
            spec = _IMPORT_SPEC_RE.match(code, start)
            specs = iter([spec] if spec else [])
        for spec in specs:
            alias, package_path = spec.groups()
            facts.imports.append(package_path)
            if package_path == "net/http":
                http_packages.add(alias or "http")
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

    for m in _HTTP_OUT_RE.finditer(masked):
        package, name = m.groups()
        if package not in http_packages:
            continue
        closing = parens.get(m.end() - 1)
        if closing is None:
            continue
        args = split_expression(code[m.end() : closing], ",")
        method = name.upper()
        url_arg = 0
        if name.startswith("NewRequest"):
            url_arg = 2 if name == "NewRequestWithContext" else 1
            if len(args) <= url_arg:
                continue
            method = _request_method(args[url_arg - 1], package)
        if not args[url_arg]:
            continue
        url = _string_literal(args[url_arg]) or "/{dynamic}"
        facts.http_calls.append(
            HttpFact(
                enclosing(m.start()),
                _line_of(text, m.start()),
                method,
                url,
                "net-http",
                f"{package}.{name}",
            )
        )
    return facts
