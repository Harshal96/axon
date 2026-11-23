"""Go extraction (pattern-based).

Covers func declarations, net/http HandleFunc/Handle, router-style
registrations (GET/POST/... methods with a path and handler), method-
prefixed patterns ("GET /path"), and net/http outbound calls.
"""

from __future__ import annotations

import json
import re
from typing import cast

from axon.v1.facts import CallFact, FileFacts, HttpFact, RouteFact, SymFact, join_paths
from axon.v1.source_ranges import delimiter_pairs, mask_source, split_expression

_FUNC_RE = re.compile(r"^func\s+(?:\(\s*\w+\s+\*?(\w+)\s*\)\s*)?(\w+)\s*\(", re.MULTILINE)
_HANDLE_RE = re.compile(r"\b(?:http\.)?Handle(?:Func)?\s*\(\s*\"([^\"]*)\"\s*,\s*([\w.]+)")
_ROUTER_RE = re.compile(
    r"\.\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Handle|HandleFunc|Mount|Route)"
    r"\s*\(\s*\"([^\"]*)\"\s*,\s*([\w.]+)?"
)
_CALL_RE = re.compile(r"(?:(\w+(?:\.\w+)*)\.)?(\w+)\s*\(")
_HTTP_OUT_RE = re.compile(r"\bhttp\.(Get|Post|Head|NewRequest(?:WithContext)?)\s*\(")
_HTTP_METHODS = "Get|Head|Post|Put|Patch|Delete|Options|Trace|Connect"
_HTTP_METHOD_CONST = re.compile(rf"http\.Method({_HTTP_METHODS})")
_IMPORT_RE = re.compile(r'"([\w./-]+)"')
_METHOD_PREFIX_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(.*)$")


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _string_literal(expression: str) -> str | None:
    if re.fullmatch(r"`[^`]*`", expression):
        return expression[1:-1].replace("\r", "")
    if re.fullmatch(r'"(?:\\.|[^"\\])*"', expression):
        try:
            return cast(str, json.loads(expression))
        except ValueError:
            return None
    return None


def _http_method(expression: str) -> str:
    value = _string_literal(expression)
    if value is not None:
        return value.upper() or "GET"  # NewRequest defaults to GET for an empty method.
    constant = _HTTP_METHOD_CONST.fullmatch(expression)
    return constant[1].upper() if constant else "UNKNOWN"


def _http_url(expression: str) -> str:
    result = ""
    parts = split_expression(expression, "+")
    for index, part in enumerate(parts):
        literal = _string_literal(part)
        if literal is not None:
            result += literal
            continue
        # Only use a variable when its surrounding literals establish a path
        # segment. Never use the prefix of an expression as the full URL.
        following = _string_literal(parts[index + 1]) if index + 1 < len(parts) else "/"
        if (
            not result.endswith("/")
            or not (result.startswith("/") or re.match(r"(?i)^https?://[^/?#{}]+/", result))
            or not following
            or not following.startswith(("/", "?", "#"))
            or not re.fullmatch(r"[A-Za-z_]\w*", part)
        ):
            return ""
        result += "{" + part + "}"
    return result


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

    for m in _HTTP_OUT_RE.finditer(masked):
        close = parens.get(m.end() - 1)
        if close is None:
            continue
        arguments = split_expression(text[m.end() : close], ",")
        call = m[1]
        method_arg = 1 if call == "NewRequestWithContext" else 0
        if call.startswith("NewRequest"):
            if len(arguments) <= method_arg + 1:
                continue
            url = _http_url(arguments[method_arg + 1])
            method = _http_method(arguments[method_arg])
        else:
            method = call.upper()
            url = _http_url(arguments[0])
        caller = enclosing(m.start())
        facts.http_calls.append(
            HttpFact(
                caller,
                _line_of(text, m.start()),
                method,
                url,
                "net-http",
                f"http.{call}",
                detail="" if url else "HTTP URL expression could not be resolved",
            )
        )
    return facts
