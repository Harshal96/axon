"""Java extraction (pattern-based, no build tooling).

Covers Spring controller annotations, Vert.x Router registrations,
declarative client interfaces, and RestTemplate/WebClient/Vert.x
WebClient outbound calls. Heuristic by design: every fact keeps its line
so reviewers can verify it.
"""

from __future__ import annotations

import re

from axon.v2.facts import BindingFact, CallFact, FileFacts, HttpFact, RouteFact, SymFact, join_paths

_CLASS_RE = re.compile(r"(?:public\s+)?(?:final\s+|abstract\s+)?(?:class|interface|enum)\s+(\w+)")
_METHOD_RE = re.compile(
    r"\b(?:public|protected|private)\s+(?:(?:static|final|synchronized|abstract|default)\s+)*"
    r"([\w.<>\[\], ?\s]+?)\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w.,\s]+)?(?=[{;])"
)
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
    named = re.search(r"\b(?:path|value)\s*=\s*(\{[^}]*\}|\"[^\"]*\")", args)
    if named:
        args = named.group(1)
    elif re.match(r"\s*\w+\s*=", args):
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
    package = re.search(r"\bpackage\s+([\w.]+)\s*;", text)
    facts.package = package.group(1) if package else ""
    facts.import_map = {name.rsplit(".", 1)[-1]: name for name in facts.imports}

    class_m = _CLASS_RE.search(text)
    cls = class_m.group(1) if class_m else path.rsplit("/", 1)[-1].removesuffix(".java")
    if class_m:
        facts.symbols.append(SymFact(cls, cls, "CLASS", _line_of(text, class_m.start()), 0))

    if class_m:
        inheritance = re.match(r"\s+extends\s+([\w.]+)", text[class_m.end() :])
        if inheritance:
            facts.superclasses[cls] = [inheritance.group(1)]

    # Class-level @RequestMapping prefix (first mapping before class decl).
    prefix = ""
    if class_m:
        head = text[: class_m.start()]
        for m in _MAPPING_RE.finditer(head):
            if m.group(1) == "RequestMapping":
                prefix = _first_string(m.group(2))

    methods: list[tuple[int, str]] = []  # (pos, name)
    for m in _METHOD_RE.finditer(text):
        name = m.group(2)
        if name in ("if", "for", "while", "switch"):
            continue
        line = _line_of(text, m.start())
        facts.symbols.append(SymFact(f"{cls}.{name}", name, "METHOD", line, line))
        methods.append((m.start(), name))
        for param in m.group(3).split(","):
            declaration = re.search(r"([\w.]+)\s+(\w+)\s*$", param)
            if declaration:
                facts.bindings.append(
                    BindingFact(declaration.group(2), declaration.group(1), f"{cls}.{name}", line)
                )
        facts.returns[f"{cls}.{name}"] = re.sub(r"\s+", "", m.group(1))

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

    def operation_id(pos: int, handler: str) -> str:
        previous = max((start for start, _ in methods if start < pos), default=body_start)
        following = next((start for start, _ in methods if start > pos), pos)
        annotations = text[previous:following]
        clean = re.sub(r'"[^"\n]*"', lambda m: " " * len(m.group()), annotations)
        annotations = annotations[clean.rfind("}") + 1 :]
        for pattern in (
            r'@Operation\s*\([^)]*operationId\s*=\s*"([^" ]+)"',
            r'@ApiOperation\s*\([^)]*nickname\s*=\s*"([^" ]+)"',
        ):
            match = re.search(pattern, annotations)
            if match:
                return match.group(1)
        return handler

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
                operation_id=operation_id(m.start(), handler),
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

    # JAX-RS annotations are grouped with their following declaration. A class
    # @Path is a prefix; a method needs an explicit HTTP verb to expose a route.
    class_path = ""
    if class_m:
        match = re.search(r'@Path\s*\(\s*"([^" ]*)"\s*\)', text[: class_m.start()])
        class_path = match.group(1) if match else ""
    previous = body_start
    for declaration in _METHOD_RE.finditer(text):
        annotations = text[previous : declaration.start()]
        # Only annotations after the preceding method body belong to this method.
        clean = re.sub(r'"[^"\n]*"', lambda m: " " * len(m.group()), annotations)
        annotation_offset = clean.rfind("}") + 1
        annotations = annotations[annotation_offset:]
        jax_verb = re.search(r"@(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", annotations)
        if jax_verb:
            path_match = re.search(r'@Path\s*\(\s*"([^" ]*)"\s*\)', annotations)
            name = declaration.group(2)
            facts.routes.append(
                RouteFact(
                    jax_verb.group(1),
                    join_paths(class_path, path_match.group(1) if path_match else ""),
                    f"{cls}.{name}",
                    _line_of(text, previous + annotation_offset + jax_verb.start()),
                    "jaxrs",
                    name,
                )
            )
        previous = declaration.end()

    # Keep complete chained receiver expressions. In particular, a chained
    # update() must never degrade to an unqualified domain-method match.
    declarations = [(m.start(), m.end()) for m in _METHOD_RE.finditer(text)]
    annotation_spans = [(m.start(), m.end()) for m in re.finditer(r"@\w+(?:\s*\([^)]*\))?", text)]
    masked = re.sub(
        r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"',
        lambda m: " " * len(m.group()),
        text,
        flags=re.DOTALL,
    )
    chain_re = re.compile(r"\b(\w+(?:\.\w+|\([^()]*\)\s*\.\s*\w+)*)\s*\(")
    utility_receivers = set()
    for declaration in re.finditer(
        r"\b(List|Map|Set|Collection|ArrayList|HashMap|HashSet|Logger)\s*(?:<[^;=()]*>)?\s+(\w+)",
        masked,
    ):
        type_name, variable = declaration.groups()
        imported = facts.import_map.get(type_name, "")
        if imported.startswith(
            ("java.util.", "java.util.logging.", "org.slf4j.", "org.apache.logging.")
        ):
            utility_receivers.add(variable)
    for m in chain_re.finditer(masked):
        if any(a <= m.start() < b for a, b in declarations + annotation_spans):
            continue
        called = re.sub(r"\s+", "", text[m.start(1) : m.end(1)])
        caller = enclosing(m.start())
        root = called.split(".", 1)[0]
        if caller and called not in {"if", "for", "while", "switch", "catch", "synchronized"}:
            if root in utility_receivers or root.lower() in {
                "log",
                "logger",
                "system",
                "collections",
                "arrays",
                "objects",
            }:
                continue
            facts.calls.append(CallFact(caller, called, _line_of(text, m.start())))

    # Declared field types and local factory assignments provide receiver evidence.
    field_re = re.compile(
        r"\b(?:private|protected|public)\s+(?:(?:static|final)\s+)*([\w.]+)(?:<[^;=]+>)?\s+(\w+)\s*(?=[;=])"
    )
    for m in field_re.finditer(text):
        facts.bindings.append(BindingFact(m.group(2), m.group(1), cls, _line_of(text, m.start())))
    for m in re.finditer(r"\b(?:[\w.<>]+\s+)?(\w+)\s*=\s*(?:new\s+)?([\w.]+)\s*\(", masked):
        facts.bindings.append(
            BindingFact(m.group(1), m.group(2), enclosing(m.start()), _line_of(text, m.start()))
        )

    for declaration in re.finditer(r"\b([\w.]+)\s+(\w+)\s*(?=[;=])", masked):
        type_name, variable = declaration.groups()
        if type_name not in {"return", "throw", "new"}:
            facts.bindings.append(
                BindingFact(
                    variable,
                    type_name,
                    enclosing(declaration.start()) or cls,
                    _line_of(text, declaration.start()),
                )
            )

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
