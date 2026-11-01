"""Java extraction (pattern-based, no build tooling).

Covers Spring controller annotations, Vert.x Router registrations,
declarative client interfaces, and RestTemplate/WebClient/Vert.x
WebClient outbound calls. Heuristic by design: every fact keeps its line
so reviewers can verify it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from axon.v1.facts import (
    BindingFact,
    CallFact,
    FileFacts,
    HttpFact,
    ParseDiagnostic,
    RouteFact,
    SymFact,
    join_paths,
)
from axon.v1.java_constants import (
    annotation_paths,
    builder_argument,
    fold,
    render_partial,
    split_expression,
    transport_path,
)
from axon.v1.source_ranges import blank, delimiter_pairs, mask_source

_CLASS_RE = re.compile(r"(?:public\s+)?(?:final\s+|abstract\s+)?(?:class|interface|enum)\s+(\w+)")
_MAPPING_RE = re.compile(
    r"@(GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping)"
    r"\s*(?:\((.*)\))?\s*$",
    re.S,
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
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+(?:\*)?);", re.MULTILINE)

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


@dataclass
class _Class:
    name: str
    qualified: str
    start: int
    body: int
    end: int


@dataclass
class _Method:
    name: str
    qualified: str
    start: int
    params: int
    params_end: int
    body: int
    end: int
    returns: str


def _annotations(masked: str, parens: dict[int, int]) -> list[tuple[int, int]]:
    spans = []
    cursor = 0
    for match in re.finditer(r"@[\w.]+", masked):
        if match.start() < cursor:
            continue
        end = match.end()
        while end < len(masked) and masked[end].isspace():
            end += 1
        cursor = parens.get(end, end - 1) + 1 if masked[end : end + 1] == "(" else match.end()
        spans.append((match.start(), cursor))
    return spans


def _scan_declarations(
    masked: str,
) -> tuple[list[_Class], list[_Method], list[tuple[int, int]], str]:
    parens = delimiter_pairs(masked, "(", ")")
    annotations = _annotations(masked, parens)
    chars = list(masked)
    for start, end in annotations:
        chars[start:end] = blank(masked[start:end])
    clean = "".join(chars)
    braces = delimiter_pairs(clean, "{", "}")
    classes: list[_Class] = []
    for match in _CLASS_RE.finditer(clean):
        body = clean.find("{", match.end())
        if body in braces:
            classes.append(_Class(match.group(1), "", match.start(), body, braces[body]))
    # Anonymous class methods belong to a distinct source-visible class scope.
    for match in re.finditer(r"\bnew\s+[\w.<>?, ]+\s*\(", clean):
        opening = clean.find("(", match.start(), match.end())
        anonymous_end = parens.get(opening)
        if anonymous_end is None:
            continue
        body = anonymous_end + 1
        while body < len(clean) and clean[body].isspace():
            body += 1
        if body in braces:
            name = f"AnonymousL{_line_of(clean, match.start())}"
            classes.append(_Class(name, "", match.start(), body, braces[body]))
    classes.sort(key=lambda c: c.start)
    for cls in classes:
        parent = max(
            (c for c in classes if c.body < cls.start < c.end), key=lambda c: c.body, default=None
        )
        cls.qualified = f"{parent.qualified}.{cls.name}" if parent else cls.name
    methods: list[_Method] = []
    for match in re.finditer(r"\b(\w+)\s*\(", clean):
        opening = match.end() - 1
        if opening not in parens:
            continue
        end = parens[opening]
        name = match.group(1)
        owner = max(
            (c for c in classes if c.body < match.start() < c.end),
            key=lambda c: c.body,
            default=None,
        )
        if owner is None:
            continue
        # A declaration is directly in its class, never in another method or lambda.
        containing = max((a for a, b in braces.items() if a < opening < b), default=-1)
        if containing != owner.body:
            continue
        boundary = max(clean.rfind(ch, owner.body, match.start()) for ch in "{};") + 1
        header = clean[boundary : match.start()].strip()
        if not header or any(ch in header for ch in "=():@"):
            continue
        if re.search(r"\b(?:return|throw|new)\b", header):
            continue
        if not re.fullmatch(r"[\w.<>\[\], ?&\s]+", header):
            continue
        tail = re.match(r"\s*(?:\[\]\s*)*(?:throws\s+[\w.,\s]+)?([;{])", clean[end + 1 :])
        if tail is None:
            continue
        body = end + 1 + tail.end() - 1
        body_end = braces.get(body, body)
        if clean[body] == "{" and body not in braces:
            continue
        start = boundary
        while start < match.start() and clean[start].isspace():
            start += 1
        returns = re.sub(
            r"\b(?:public|protected|private|static|final|synchronized|abstract|default|native|strictfp)\b",
            "",
            header,
        )
        methods.append(
            _Method(
                name,
                f"{owner.qualified}.{name}",
                start,
                opening,
                end,
                body,
                body_end,
                re.sub(r"\s+", "", returns),
            )
        )
    return classes, methods, annotations, clean


def parse_java(path: str, text: str) -> FileFacts:
    facts = FileFacts(path=path, language="java")
    masked = mask_source(text)
    facts.imports = _IMPORT_RE.findall(masked)
    package = re.search(r"\bpackage\s+([\w.]+)\s*;", masked)
    facts.package = package.group(1) if package else ""
    facts.import_map = {name.rsplit(".", 1)[-1]: name for name in facts.imports}
    classes, methods, annotation_spans, clean = _scan_declarations(masked)
    cls = classes[0].qualified if classes else path.rsplit("/", 1)[-1].removesuffix(".java")
    for scope in classes:
        body_text = text[scope.body : scope.end]
        if re.search(r"(?:^|[/.])models?(?:[/.]|$)", path) or re.search(
            r"@(JsonProperty|SerializedName)\b", body_text
        ):
            facts.class_roles[scope.qualified] = "model"
        elif scope.name.endswith(("Api", "API")) or ".api" in facts.package:
            facts.class_roles[scope.qualified] = "api"
        facts.symbols.append(
            SymFact(
                scope.qualified,
                scope.name,
                "CLASS",
                _line_of(text, scope.start),
                _line_of(text, scope.end),
            )
        )
        inheritance = re.search(r"\bextends\s+([\w.]+)", clean[scope.start : scope.body])
        bases = [inheritance.group(1)] if inheritance else []
        implemented = re.search(r"\bimplements\s+([\w.,\s]+)", clean[scope.start : scope.body])
        if implemented:
            bases.extend(part.strip() for part in implemented.group(1).split(",") if part.strip())
        if bases:
            facts.superclasses[scope.qualified] = bases
    for declaration in methods:
        line = _line_of(text, declaration.start)
        facts.symbols.append(
            SymFact(
                declaration.qualified,
                declaration.name,
                "METHOD",
                line,
                _line_of(text, declaration.end),
            )
        )
        for param in clean[declaration.params + 1 : declaration.params_end].split(","):
            binding = re.search(r"([\w.]+)\s+(\w+)\s*$", param)
            if binding:
                facts.bindings.append(
                    BindingFact(binding.group(2), binding.group(1), declaration.qualified, line)
                )
        facts.returns[declaration.qualified] = declaration.returns

    def class_at(pos: int) -> str:
        owner = max(
            (c for c in classes if c.body < pos < c.end), key=lambda c: c.body, default=None
        )
        return owner.qualified if owner else ""

    def enclosing(pos: int) -> str:
        owner = max(
            (m for m in methods if m.body < pos < m.end and clean[m.body] == "{"),
            key=lambda m: m.body,
            default=None,
        )
        # Methods in an enclosing class do not own nested class initializers.
        class_owner = class_at(pos)
        if owner and owner.qualified.rsplit(".", 1)[0] == class_owner:
            return owner.qualified
        return class_owner

    def annotations_before(pos: int) -> list[tuple[int, int]]:
        selected: list[tuple[int, int]] = []
        cursor = pos
        for start, end in reversed(annotation_spans):
            if end > cursor:
                continue
            if clean[end:cursor].strip():
                break
            selected.append((start, end))
            cursor = start
        return list(reversed(selected))

    constants: dict[str, str] = {}
    for match in re.finditer(
        r"\b(?:static\s+final\s+|final\s+static\s+)?String\s+(\w+)\s*=([^;]+);", text
    ):
        if not masked[match.start() : match.start() + 1].strip():
            continue
        # Only class-level constants, never local request variables.
        if any(m.body < match.start() < m.end for m in methods):
            continue
        owner = class_at(match.start())
        owner_decl = next((c for c in classes if c.qualified == owner), None)
        implicit_final = (
            owner_decl is not None and "interface" in clean[owner_decl.start : owner_decl.body]
        )
        if not implicit_final and not ("static" in match.group() and "final" in match.group()):
            continue
        constants[owner + "." + match.group(1)] = match.group(2).strip()

    def paths(args: str | None, owner: str, line: int) -> list[str]:
        scoped = dict(constants)
        for key, value in constants.items():
            prefix, _, leaf = key.rpartition(".")
            if (
                owner == prefix
                or owner.startswith(prefix + ".")
                or prefix in facts.superclasses.get(owner, [])
            ):
                scoped[leaf] = value
        values = annotation_paths(args, scoped)
        if any("{java:" in value or "{unresolved}" in value for value in values):
            facts.diagnostics.append(
                ParseDiagnostic(
                    line, "route-path-partial", "route-path-partial: unresolved annotation operand"
                )
            )
        return values

    for key, expression in constants.items():
        scoped = {
            **constants,
            **{
                k.rsplit(".", 1)[-1]: v
                for k, v in constants.items()
                if k.rpartition(".")[0] == key.rpartition(".")[0]
            },
        }
        facts.constants[key] = [fold(expression, scoped)]

    prefixes: dict[str, list[str]] = {}
    class_paths: dict[str, list[str]] = {}
    assigned: set[int] = set()
    for scope in classes:
        for start, end in annotations_before(scope.start):
            raw = text[start:end]
            mapping = _MAPPING_RE.match(raw)
            if mapping and mapping.group(1) == "RequestMapping":
                prefixes[scope.qualified] = paths(
                    mapping.group(2), scope.qualified, _line_of(text, start)
                )
                assigned.add(start)
            path_match = re.match(r"@Path\s*\((.*)\)\s*$", raw, re.S)
            if path_match:
                class_paths[scope.qualified] = paths(
                    path_match.group(1), scope.qualified, _line_of(text, start)
                )
    for declaration in methods:
        owner = declaration.qualified.rsplit(".", 1)[0]
        spans = annotations_before(declaration.start)
        raw_annotations = "\n".join(text[a:b] for a, b in spans)
        operation = re.search(r'@Operation\s*\([^)]*operationId\s*=\s*"([^" ]+)"', raw_annotations)
        if operation is None:
            operation = re.search(
                r'@ApiOperation\s*\([^)]*nickname\s*=\s*"([^" ]+)"', raw_annotations
            )
        for start, end in spans:
            mapping = _MAPPING_RE.match(text[start:end])
            if mapping:
                assigned.add(start)
                ann = mapping.group(1)
                for prefix in prefixes.get(owner, [""]):
                    for path in paths(mapping.group(2), owner, _line_of(text, start)):
                        template = join_paths(prefix, path)
                        facts.routes.append(
                            RouteFact(
                                _METHOD_BY_ANNOTATION.get(ann) or _request_method(mapping.group(2)),
                                render_partial(template),
                                declaration.qualified,
                                _line_of(text, start),
                                "spring",
                                operation.group(1) if operation else declaration.name,
                                path_template=template,
                            )
                        )
        jax_verb = re.search(r"@(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", raw_annotations)
        if jax_verb:
            path_match = re.search(r"@Path\s*\((.*?)\)", raw_annotations, re.S)
            line = _line_of(text, spans[0][0])
            for prefix in class_paths.get(owner, [""]):
                for path in paths(path_match.group(1) if path_match else "", owner, line):
                    template = join_paths(prefix, path)
                    facts.routes.append(
                        RouteFact(
                            jax_verb.group(1),
                            render_partial(template),
                            declaration.qualified,
                            line,
                            "jaxrs",
                            declaration.name,
                            path_template=template,
                        )
                    )
        request_line = re.search(
            r'@RequestLine\s*\(\s*"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+([^" ]+)"',
            raw_annotations,
        )
        if request_line:
            facts.http_calls.append(
                HttpFact(
                    declaration.qualified,
                    _line_of(text, spans[0][0]),
                    request_line.group(1),
                    request_line.group(2),
                    "generated-source",
                    declaration.qualified,
                )
            )
    for start, end in annotation_spans:
        if start not in assigned and _MAPPING_RE.match(text[start:end]):
            facts.diagnostics.append(
                ParseDiagnostic(
                    _line_of(text, start),
                    "suspect-handler",
                    "mapping annotation is not adjacent to a method or class declaration",
                )
            )

    for m in _VERTX_ROUTE_RE.finditer(text):
        if not masked[m.start() : m.start() + 1].strip():
            continue
        ref = m.group(3)
        owner = class_at(m.start()) or cls
        caller = f"{owner}.{ref}" if ref else (enclosing(m.start()) or owner)
        facts.routes.append(
            RouteFact(
                m.group(1).upper() if m.group(1).lower() != "route" else "ANY",
                join_paths("", m.group(2)),
                caller,
                _line_of(text, m.start()),
                "vertx",
                caller.rsplit(".", 1)[-1],
            )
        )

    declarations = [(m.start, m.body + 1) for m in methods]
    chain_re = re.compile(r"\b(\w+(?:\.\w+|\([^()]*\)\s*\.\s*\w+)*)\s*\(")
    utility_receivers = set()
    for variable_match in re.finditer(
        r"\b(List|Map|Set|Collection|ArrayList|HashMap|HashSet|Logger)\s*(?:<[^;=()]*>)?\s+(\w+)",
        masked,
    ):
        type_name, variable = variable_match.groups()
        imported = facts.import_map.get(type_name, "")
        if imported.startswith(
            ("java.util.", "java.util.logging.", "org.slf4j.", "org.apache.logging.")
        ):
            utility_receivers.add(variable)
    call_parens = delimiter_pairs(masked, "(", ")")
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
            opening = m.end() - 1
            closing = call_parens.get(opening, opening + 1)
            args_text = text[opening + 1 : closing]
            arguments = split_expression(args_text, ",") if args_text.strip() else []
            facts.calls.append(
                CallFact(
                    caller,
                    called,
                    _line_of(text, m.start()),
                    arguments={
                        str(i): fold(arg, {}) if arg.startswith('"') else ""
                        for i, arg in enumerate(arguments)
                    },
                    argument_count=len(arguments),
                )
            )

    # Declared field types and local factory assignments provide receiver evidence.
    field_re = re.compile(
        r"\b(?:private|protected|public)\s+(?:(?:static|final)\s+)*([\w.]+)(?:<[^;=]+>)?\s+(\w+)\s*(?=[;=])"
    )
    for m in field_re.finditer(text):
        facts.bindings.append(
            BindingFact(
                m.group(2), m.group(1), class_at(m.start()) or cls, _line_of(text, m.start())
            )
        )
    for m in re.finditer(r"\b(?:[\w.<>]+\s+)?(\w+)\s*=\s*(?:new\s+)?([\w.]+)\s*\(", masked):
        facts.bindings.append(
            BindingFact(m.group(1), m.group(2), enclosing(m.start()), _line_of(text, m.start()))
        )

    for variable_match in re.finditer(r"\b([\w.]+)\s+(\w+)\s*(?=[;=])", masked):
        type_name, variable = variable_match.groups()
        if type_name not in {"return", "throw", "new"}:
            facts.bindings.append(
                BindingFact(
                    variable,
                    type_name,
                    enclosing(variable_match.start()) or cls,
                    _line_of(text, variable_match.start()),
                )
            )

    # OpenAPI generator transports: path and verb must occur in the same method.
    for declaration in methods:
        body = text[declaration.body : declaration.end]
        body_mask = masked[declaration.body : declaration.end]
        path_match = re.search(r"\bString\s+localVarPath\s*=", body_mask)
        if path_match:
            end = body_mask.find(";", path_match.end())
            expression = body[path_match.end() : end] if end >= 0 else ""
            if re.match(r"\s*UriComponentsBuilder\.fromPath", mask_source(expression)):
                expression = builder_argument(expression)
        else:
            path_match = re.search(r"\bUriComponentsBuilder\.fromPath\s*\(", body_mask)
            if path_match is None:
                continue
            end = body_mask.find(";", path_match.end())
            expression = (
                builder_argument(body[path_match.start() : end]) if end >= 0 else "{unresolved}"
            )
        path_value = transport_path(
            expression,
            {
                **constants,
                **{
                    k.rsplit(".", 1)[-1]: v
                    for k, v in constants.items()
                    if k.rpartition(".")[0] == declaration.qualified.rpartition(".")[0]
                },
            },
        )
        if not path_value.startswith("/") or "{java:" in path_value or "{unresolved}" in path_value:
            facts.diagnostics.append(
                ParseDiagnostic(
                    _line_of(text, declaration.body + path_match.start()),
                    "transport-path-partial",
                    "generated transport path is not statically complete",
                )
            )
            continue
        patterns = [
            r'\blocalVarRequestBuilder\.method\s*\(\s*"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)"',
            r"\blocalVarRequestBuilder\.(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*\(",
            r'\b\w+\.buildCall\s*\([^;]*?\blocalVarPath\s*,\s*"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)"',
            r"\b\w+\.invokeAPI\s*\([^;]*?HttpMethod\.(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b",
        ]
        for pattern in patterns:
            verb = next(
                (
                    m
                    for m in re.finditer(pattern, body)
                    if body_mask[m.start() : m.start() + 1].strip()
                ),
                None,
            )
            if verb:
                facts.http_calls.append(
                    HttpFact(
                        declaration.qualified,
                        _line_of(text, declaration.body + path_match.start()),
                        verb.group(1),
                        path_value,
                        "generated-source",
                        declaration.qualified,
                    )
                )
                break

    # Outbound HTTP.
    for m in _RESTTEMPLATE_RE.finditer(text):
        if not masked[m.start() : m.start() + 1].strip():
            continue
        transport_verb = m.group(1)
        method = (
            "GET"
            if transport_verb.startswith("get")
            else "POST"
            if transport_verb.startswith("post")
            else ""
        )
        facts.http_calls.append(
            HttpFact(
                enclosing(m.start()),
                _line_of(text, m.start()),
                method,
                m.group(2),
                "resttemplate",
                transport_verb,
            )
        )
    for m in _WEBCLIENT_RE.finditer(text):
        if not masked[m.start() : m.start() + 1].strip():
            continue
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
        if not masked[m.start() : m.start() + 1].strip():
            continue
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
