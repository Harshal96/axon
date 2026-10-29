"""Java extraction (pattern-based, no build tooling).

Covers Spring controller annotations, Vert.x Router registrations,
declarative client interfaces, and RestTemplate/WebClient/Vert.x
WebClient outbound calls. Heuristic by design: every fact keeps its line
so reviewers can verify it.
"""

from __future__ import annotations

import re

from axon.v2.facts import CallFact, FileFacts, HttpFact, RouteFact, SymFact, join_paths

_CLASS_RE = re.compile(r"(?:public\s+)?(?:final\s+|abstract\s+)?(?:class|interface|enum)\s+(\w+)")
_METHOD_RE = re.compile(r"(?:public|protected|private)\s+(?:static\s+)?[\w<>\[\], ?]+\s+(\w+)\s*\(")
_MAPPING_RE = re.compile(
    r"@(GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping)"
    r"\s*(?:\(([^)]*)\))?"
)
_STR_RE = re.compile(r'"([^"]*)"')
_VERTX_ROUTE_RE = re.compile(
    r"\brouter\.(get|post|put|patch|delete|route)\s*\(\s*\"([^\"]*)\""
    r"(?:[^;]*?\.handler\s*\(\s*(?:this::)?(\w+))?",
    re.IGNORECASE,
)
_CALL_RE = re.compile(r"(?:(\w+(?:\.\w+)*)\.)?(\w+)\s*\(")
_RESTTEMPLATE_RE = re.compile(
    r"\.(getForObject|getForEntity|postForObject|postForEntity|exchange|execute)\s*\(\s*\"([^\"]*)\""
)
_WEBCLIENT_RE = re.compile(
    r"\.(get|post|put|patch|delete)\s*\(\s*\)\s*\.\s*uri\s*\(\s*\"([^\"]*)\"", re.IGNORECASE
)
_VERTX_CLIENT_RE = re.compile(
    r"\bclient\.(get|post|put|patch|delete)\s*\(\s*(?:\d+\s*,\s*\"[^\"]*\"\s*,\s*)?\"([^\"]*)\"",
    re.IGNORECASE,
)
_IMPORT_RE = re.compile(r"^import\s+([\w.]+);", re.MULTILINE)

_METHOD_BY_ANNOTATION = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "PatchMapping": "PATCH",
    "DeleteMapping": "DELETE",
}


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _first_string(args: str | None) -> str:
    if not args:
        return ""
    m = _STR_RE.search(args)
    return m.group(1) if m else ""


def _request_method(args: str | None) -> str:
    if not args:
        return "ANY"
    m = re.search(r"RequestMethod\.(\w+)", args)
    return m.group(1).upper() if m else "ANY"


def parse_java(path: str, text: str) -> FileFacts:
    facts = FileFacts(path=path, language="java")
    facts.imports = _IMPORT_RE.findall(text)

    class_m = _CLASS_RE.search(text)
    cls = class_m.group(1) if class_m else path.rsplit("/", 1)[-1].removesuffix(".java")
    if class_m:
        facts.symbols.append(SymFact(cls, cls, "CLASS", _line_of(text, class_m.start()), 0))

    # Class-level @RequestMapping prefix (first mapping before class decl).
    prefix = ""
    if class_m:
        head = text[: class_m.start()]
        for m in _MAPPING_RE.finditer(head):
            if m.group(1) == "RequestMapping":
                prefix = _first_string(m.group(2))

    methods: list[tuple[int, str]] = []  # (pos, name)
    for m in _METHOD_RE.finditer(text):
        name = m.group(1)
        if name in ("if", "for", "while", "switch"):
            continue
        line = _line_of(text, m.start())
        facts.symbols.append(SymFact(f"{cls}.{name}", name, "METHOD", line, line))
        methods.append((m.start(), name))

    def method_at(pos: int) -> str:
        """Nearest method starting at or after pos (annotations precede)."""
        for mpos, name in methods:
            if mpos >= pos:
                return name
        return ""

    def enclosing(pos: int) -> str:
        cur = ""
        for mpos, name in methods:
            if mpos <= pos:
                cur = name
        return f"{cls}.{cur}" if cur else ""

    # Spring mappings after the class declaration.
    body_start = class_m.end() if class_m else 0
    for m in _MAPPING_RE.finditer(text, body_start):
        ann = m.group(1)
        handler = method_at(m.end())
        if not handler:
            continue
        method = _METHOD_BY_ANNOTATION.get(ann) or _request_method(m.group(2))
        facts.routes.append(
            RouteFact(
                method=method,
                path=join_paths(prefix, _first_string(m.group(2))),
                handler=f"{cls}.{handler}",
                line=_line_of(text, m.start()),
                framework="spring",
                operation_id=handler,
            )
        )

    # Vert.x router registrations -> handler is the enclosing method.
    for m in _VERTX_ROUTE_RE.finditer(text):
        ref = m.group(3)
        caller = f"{cls}.{ref}" if ref else (enclosing(m.start()) or cls)
        facts.routes.append(
            RouteFact(
                method=m.group(1).upper() if m.group(1).lower() != "route" else "ANY",
                path=join_paths("", m.group(2)),
                handler=caller,
                line=_line_of(text, m.start()),
                framework="vertx",
                operation_id=caller.rsplit(".", 1)[-1],
            )
        )

    # Generic calls (for CALLS edges).
    for m in _CALL_RE.finditer(text):
        caller = enclosing(m.start())
        called = f"{m.group(1)}.{m.group(2)}" if m.group(1) else m.group(2)
        if caller and m.group(2) not in ("if", "for", "while", "return"):
            facts.calls.append(CallFact(caller, called, _line_of(text, m.start())))

    # Outbound HTTP.
    for m in _RESTTEMPLATE_RE.finditer(text):
        verb = m.group(1)
        method = "GET" if verb.startswith("get") else "POST" if verb.startswith("post") else ""
        facts.http_calls.append(
            HttpFact(
                enclosing(m.start()),
                _line_of(text, m.start()),
                method,
                m.group(2),
                "resttemplate",
                verb,
            )
        )
    for m in _WEBCLIENT_RE.finditer(text):
        facts.http_calls.append(
            HttpFact(
                enclosing(m.start()),
                _line_of(text, m.start()),
                m.group(1).upper(),
                m.group(2),
                "webclient",
                m.group(1),
            )
        )
    for m in _VERTX_CLIENT_RE.finditer(text):
        facts.http_calls.append(
            HttpFact(
                enclosing(m.start()),
                _line_of(text, m.start()),
                m.group(1).upper(),
                m.group(2),
                "vertx-webclient",
                m.group(1),
            )
        )
    return facts
