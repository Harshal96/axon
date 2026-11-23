"""Bounded JavaScript and TypeScript HTTP extraction for named functions."""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from axon.v1.facts import CallFact, FileFacts, HttpFact, SymFact
from axon.v1.fingerprints import JS_LANGUAGES
from axon.v1.sdk_catalog import url_shaped
from axon.v1.source_ranges import blank, delimiter_pairs, split_expression

_IDENT = r"[$A-Za-z_][$\w]*"
_FUNCTION = re.compile(
    rf"(?<![$\w.])(?:function\s+(?P<fn>{_IDENT})|"
    rf"(?:const|let|var)\s+(?P<arrow>{_IDENT})\s*=\s*(?:async\s+)?)\s*\("
)
_BODY = re.compile(r"\s*(?::\s*[\w$.[\]<>| ,]+)?\s*(?P<arrow>=>\s*)?\{")
_CALL = re.compile(rf"(?<![$\w.])({_IDENT})(?:\s*\.\s*({_IDENT}))?\s*\(")
_IMPORT = re.compile(r"\bimport\s+([^;\n]+?)\s+from\s+(['\"])([^'\"]+)\2")
_AXIOS_REQUIRE = re.compile(
    rf"\b(?:const|let|var)\s+({_IDENT})\s*=\s*require\s*\(\s*['\"]axios['\"]\s*\)"
)
_BINDINGS = re.compile(rf"\b(?:const|let|var|function|class)\s+({_IDENT})")
_LITERAL = re.compile(r"""(?:"([^"\\\n]*)"|'([^'\\\n]*)'|`([^`\\\n]*)`)""")
_HTTP_METHODS = {"get", "head", "post", "put", "patch", "delete", "options", "trace", "connect"}
# A slash in expression-start position can open a regular expression.
# Keep the preceding token so scanning and delimiter offsets stay aligned.
_HIDDEN = re.compile(
    r"""//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`"""
    r"|(?P<prefix>[=(:,;!?&|{}\[]\s*|\b(?:return|throw|case)\s+)"
    r"/(?![*/])(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\[\n])+/[a-z]*"
)


@dataclass
class _Function:
    name: str
    start: int
    end: int
    bound_names: set[str]


def _literal(value: str) -> str:
    literal = _LITERAL.fullmatch(value)
    if literal is None:
        return ""
    return next((part for part in literal.groups() if part and "${" not in part), "")


def _fetch_method(arguments: list[str], url: str) -> str:
    if not url:
        return "UNKNOWN"
    if len(arguments) < 2 or arguments[1] in {"", "null", "undefined"}:
        return "GET"
    options = arguments[1]
    if not options.startswith("{") or not options.endswith("}"):
        return "UNKNOWN"
    method = "get"
    for item in split_expression(options[1:-1], ","):
        if not item:
            continue
        key, separator, value = item.partition(":")
        key = key.strip()
        name = key if re.fullmatch(_IDENT, key) else _literal(key)
        # Spread/computed/getter properties can change the request method.
        if not name:
            return "UNKNOWN"
        if name == "method":
            method = _literal(value.strip()).lower() if separator else ""
    return method.upper() if method in _HTTP_METHODS else "UNKNOWN"


def parse_javascript(path: str, text: str) -> FileFacts:
    facts = FileFacts(path=path, language=JS_LANGUAGES[Path(path).suffix])
    masked = _HIDDEN.sub(
        lambda m: (m["prefix"] or "") + blank(m[0][len(m["prefix"] or "") :]), text
    )
    braces = delimiter_pairs(masked, "{", "}")
    parens = delimiter_pairs(masked, "(", ")")
    lines = [m.start() for m in re.finditer("\n", text)]

    def line(pos: int) -> int:
        return 1 + bisect_right(lines, pos)

    functions: list[_Function] = []
    declaration_offsets = set()
    for match in _FUNCTION.finditer(masked):
        opening = match.end() - 1
        end_parameters = parens.get(opening)
        if end_parameters is None:
            continue
        body = _BODY.match(masked, end_parameters + 1)
        if body is None or bool(match["arrow"]) != bool(body["arrow"]):
            continue
        body_start = body.end() - 1
        if body_start not in braces:
            continue
        name = match["fn"] or match["arrow"]
        body_end = braces[body_start]
        params = set(re.findall(_IDENT, masked[opening + 1 : end_parameters]))
        functions.append(_Function(name, body_start, body_end, params))
        declaration_offsets.add(opening)
        facts.symbols.append(SymFact(name, name, "FUNCTION", line(match.start()), line(body_end)))

    axios_names: set[str] = set()
    module_names: set[str] = set()
    # The bindings are valid only when the matching syntax is outside literals/comments.
    for match in _IMPORT.finditer(text):
        if masked.startswith("import", match.start()):
            module_names.update(re.findall(_IDENT, match[1]))
            if match[3] == "axios" and re.fullmatch(_IDENT, match[1]):
                axios_names.add(match[1])
    requires = {
        match.start(): match[1]
        for match in _AXIOS_REQUIRE.finditer(text)
        if masked[match.start()].strip()
        and not any(start < match.start() < end for start, end in braces.items())
    }
    axios_names.update(requires.values())
    functions.sort(key=lambda fn: fn.start)
    top_level: list[_Function] = []
    for fn in functions:
        if not top_level or top_level[-1].end < fn.start:
            top_level.append(fn)
    top_starts = [fn.start for fn in top_level]
    for match in _BINDINGS.finditer(masked):
        top_index = bisect_right(top_starts, match.start()) - 1
        inside_function = top_index >= 0 and match.start() < top_level[top_index].end
        if match.start() not in requires and not inside_function:
            module_names.add(match[1])
    for fn in functions:
        fn.bound_names.update(
            match[1] for match in _BINDINGS.finditer(masked, fn.start + 1, fn.end)
        )

    local_names = {fn.name for fn in functions}
    active: list[_Function] = []
    next_function = 0
    for match in _CALL.finditer(masked):
        while next_function < len(functions) and functions[next_function].start < match.start():
            fn = functions[next_function]
            while active and active[-1].end < fn.start:
                active.pop()
            active.append(fn)
            next_function += 1
        while active and active[-1].end < match.start():
            active.pop()
        opening = match.end() - 1
        if not active or opening in declaration_offsets or opening not in parens:
            continue
        caller = active[-1].name
        root, member = match[1], match[2] or ""
        if any(root in fn.bound_names for fn in active):
            continue
        if root in local_names and not member:
            facts.calls.append(CallFact(caller, root, line(match.start())))
            continue

        if (root == "fetch" and not member and root not in module_names) or (
            root in {"globalThis", "window"} and member == "fetch" and root not in module_names
        ):
            transport = "fetch"
        elif root in axios_names and member in _HTTP_METHODS:
            transport = "axios"
        else:
            continue
        args = split_expression(text[opening + 1 : parens[opening]], ",")
        url = _literal(args[0])
        if not url_shaped(url):
            url = ""
        if transport == "axios":
            # Axios instances can supply an unseen origin for relative paths.
            if not url.lower().startswith(("https://", "http://")):
                url = ""
            method = member.upper() if url else "UNKNOWN"
        else:
            method = _fetch_method(args, url)
        facts.http_calls.append(
            HttpFact(
                caller,
                line(match.start()),
                method,
                url,
                transport,
                f"{root}.{member}" if member else root,
            )
        )
    return facts
