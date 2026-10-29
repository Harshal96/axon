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


@dataclass
class RouteFact:
    method: str
    path: str
    handler: str  # qualified name
    line: int
    framework: str
    operation_id: str = ""


@dataclass
class HttpFact:
    caller: str
    line: int
    method: str = ""  # GET/POST/... or "" when unknown
    url: str = ""  # url/path template as written
    via: str = ""  # httpx | requests | generated-client | resttemplate | ...
    call_name: str = ""  # e.g. PaymentClient.create_charge


MAX_DIAGNOSTICS_PER_FILE = 10


@dataclass
class ParseDiagnostic:
    line: int
    category: str  # e.g. "SyntaxWarning" (wording depends on interpreter version)
    message: str  # e.g. "invalid escape sequence '\\d'"


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
    diagnostics_truncated: int = 0  # diagnostics dropped past the per-file cap


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
    )


def normalize_path(path: str) -> str:
    """Normalize route templates across frameworks to /a/{id} form."""
    p = path.strip()
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
