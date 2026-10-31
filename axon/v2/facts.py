"""Per-file extracted facts. Parsers are pure: text in, facts out."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SymFact:
    qualified: str  # Class.method or function
    name: str
    kind: str  # FUNCTION | METHOD | CLASS
    line_start: int
    line_end: int
    signature: str = ""
    params: list[str] = field(default_factory=list)  # positional parameter names


@dataclass
class CallFact:
    caller: str  # qualified name of containing symbol ("" = module level)
    called: str  # dotted call target as written, e.g. "client.charge"
    line: int
    args: str = ""  # raw first-argument text when cheap to capture
    # (position, dotted name) for arguments passed uninvoked, e.g.
    # call_payments_api(payments_api.v1_payment_list) -> [(0, "payments_api.v1_...")]
    callable_args: list[tuple[int, str]] = field(default_factory=list)
    arguments: dict[str, str] = field(default_factory=dict)  # position or keyword -> static string
    arg_refs: list[tuple[int | str, str]] = field(default_factory=list)
    rule: str = ""


@dataclass
class BindingFact:
    var: str  # variable assigned the result of a call
    source: str  # dotted callee, e.g. "settings.GET_PAYMENTS_API"
    caller: str  # containing symbol ("" = module level)
    line: int


@dataclass
class IncludeFact:
    prefix: str  # url prefix the module is mounted under, e.g. "api/v1"
    module: str  # dotted module as written, e.g. "orders.urls"
    line: int
    symbol: str = ""
    kind: str = "django"
    router: str = ""


@dataclass
class RouterRegistrationFact:
    router: str
    prefix: str
    viewset: str
    line: int


@dataclass
class RouteFact:
    method: str
    path: str
    handler: str  # qualified name
    line: int
    framework: str
    operation_id: str = ""
    router: str = ""


@dataclass
class HttpFact:
    caller: str
    line: int
    method: str = ""  # GET/POST/... or "" when unknown
    url: str = ""  # url/path template as written
    via: str = ""  # httpx | requests | generated-client | resttemplate | ...
    call_name: str = ""  # e.g. PaymentClient.create_charge
    url_param: str = ""  # source-visible HTTP wrapper parameter
    method_param: str = ""
    base_ref: str = ""
    detail: str = ""
    ambiguous: bool = False
    client_id: str = ""
    client_owner: str = ""


MAX_DIAGNOSTICS_PER_FILE = 10


@dataclass
class ParseDiagnostic:
    line: int
    category: str  # e.g. "SyntaxWarning" (wording depends on interpreter version)
    message: str  # e.g. "invalid escape sequence '\\d'"


@dataclass
class SignalFact:
    receiver: str
    signal: str
    sender: str
    line: int


@dataclass
class FileFacts:
    path: str  # repo-relative
    language: str
    symbols: list[SymFact] = field(default_factory=list)
    calls: list[CallFact] = field(default_factory=list)
    routes: list[RouteFact] = field(default_factory=list)
    http_calls: list[HttpFact] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    bindings: list[BindingFact] = field(default_factory=list)
    includes: list[IncludeFact] = field(default_factory=list)
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)
    suppressed_calls: int = 0
    diagnostics_truncated: int = 0  # diagnostics dropped past the per-file cap
    package: str = ""
    import_map: dict[str, str] = field(default_factory=dict)
    superclasses: dict[str, list[str]] = field(default_factory=dict)
    returns: dict[str, str] = field(default_factory=dict)
    returns_inferred: dict[str, list[str]] = field(default_factory=dict)
    constants: dict[str, list[str]] = field(default_factory=dict)
    signals: list[SignalFact] = field(default_factory=list)
    pattern_routes: dict[str, list[RouteFact]] = field(default_factory=dict)
    view_actions: dict[str, list[RouteFact]] = field(default_factory=dict)
    router_registrations: list[RouterRegistrationFact] = field(default_factory=list)


def facts_to_dict(facts: FileFacts) -> dict[str, Any]:
    """JSON-safe dict for the facts cache (all leaves are scalars)."""
    return asdict(facts)


def facts_from_dict(data: dict[str, Any]) -> FileFacts:
    """Rebuild facts, tolerating missing fields from older cache entries."""
    if not isinstance(data, dict):
        raise ValueError("facts payload is not an object")
    for key in ("symbols", "calls", "routes", "http_calls", "bindings"):
        if key in data and not isinstance(data[key], list):
            raise ValueError(f"facts field {key} is not a list")
    if "diagnostics" in data and not isinstance(data["diagnostics"], list):
        raise ValueError("facts field diagnostics is not a list")

    def _call(c: dict[str, Any]) -> CallFact:
        c = dict(c)
        c["callable_args"] = [(int(i), str(d)) for i, d in c.get("callable_args", [])]
        c["arg_refs"] = [(i, str(d)) for i, d in c.get("arg_refs", [])]
        return CallFact(**c)

    return FileFacts(
        path=str(data.get("path", "")),
        language=str(data.get("language", "")),
        symbols=[SymFact(**s) for s in data.get("symbols", [])],
        calls=[_call(c) for c in data.get("calls", [])],
        routes=[RouteFact(**r) for r in data.get("routes", [])],
        http_calls=[HttpFact(**h) for h in data.get("http_calls", [])],
        imports=[str(i) for i in data.get("imports", [])],
        bindings=[BindingFact(**b) for b in data.get("bindings", [])],
        includes=[IncludeFact(**i) for i in data.get("includes", [])],
        diagnostics=[ParseDiagnostic(**d) for d in data.get("diagnostics", [])],
        diagnostics_truncated=int(data.get("diagnostics_truncated", 0)),
        suppressed_calls=int(data.get("suppressed_calls", 0)),
        package=str(data.get("package", "")),
        import_map=dict(data.get("import_map", {})),
        superclasses=dict(data.get("superclasses", {})),
        returns=dict(data.get("returns", {})),
        returns_inferred=dict(data.get("returns_inferred", {})),
        constants=dict(data.get("constants", {})),
        signals=[SignalFact(**v) for v in data.get("signals", [])],
        router_registrations=[
            RouterRegistrationFact(**registration)
            for registration in data.get("router_registrations", [])
        ],
        view_actions={
            name: [RouteFact(**route) for route in routes]
            for name, routes in data.get("view_actions", {}).items()
        },
        pattern_routes={
            name: [RouteFact(**route) for route in routes]
            for name, routes in data.get("pattern_routes", {}).items()
        },
    )


def _normalize_named_groups(path: str) -> str:
    """Replace regex named groups, respecting nested groups and character classes."""
    output: list[str] = []
    position = 0
    while match := re.search(r"\(\?P<([^>]+)>", path[position:]):
        start = position + match.start()
        cursor = position + match.end()
        depth, in_class, escaped = 1, False, False
        while cursor < len(path) and depth:
            char = path[cursor]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif not in_class and char == "(":
                depth += 1
            elif not in_class and char == ")":
                depth -= 1
            cursor += 1
        if depth:
            break
        output.extend([path[position:start], "{" + match.group(1) + "}"])
        position = cursor
    output.append(path[position:])
    return "".join(output)


def normalize_path(path: str) -> str:
    """Normalize route templates across frameworks to /a/{id} form."""
    p = _normalize_named_groups(path.strip())
    if not p.startswith("/"):
        p = "/" + p
    # Django converters: <int:id>, <id>
    p = re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", r"{\1}", p)
    # Go/echo style :id and *rest
    p = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"{\1}", p)
    p = re.sub(r"\*([A-Za-z_][A-Za-z0-9_]*)", r"{\1}", p)
    # Strip regex anchors left over from Django re_path
    p = p.replace("^", "").replace("$", "")
    p = re.sub(r"\(\?P<([^>]+)>[^)]*\)", r"{\1}", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"


def join_paths(prefix: str, path: str) -> str:
    a = (prefix or "").strip()
    b = (path or "").strip()
    if a and not a.startswith("/"):
        a = "/" + a
    if b and not b.startswith("/"):
        b = "/" + b
    joined = (a.rstrip("/") + b) if a else b
    return normalize_path(joined or "/")


def path_signature(path: str) -> str:
    """Template-insensitive signature: /v1/charges/{id} -> /v1/charges/{}"""
    return re.sub(r"\{[^}]+\}", "{}", normalize_path(path))
