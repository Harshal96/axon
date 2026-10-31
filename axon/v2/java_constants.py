"""Conservative folding for Java string annotation expressions; never executes code."""

from __future__ import annotations

import json
import re

from axon.v2.facts import FileFacts, ParseDiagnostic
from axon.v2.source_ranges import blank, delimiter_pairs, mask_source

_TOKEN = re.compile(r"\{java:([\w.]+)\}")


def split_expression(value: str, delimiter: str) -> list[str]:
    masked = mask_source(value)
    depth = 0
    result = []
    start = 0
    for i, char in enumerate(masked):
        if char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
        elif char == delimiter and depth == 0:
            result.append(value[start:i].strip())
            start = i + 1
    result.append(value[start:].strip())
    return result


def _without_comments(value: str) -> str:
    # Tokenize literals and comments together; comment-looking literal content stays.
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*', re.S)
    return tokens.sub(
        lambda m: blank(m.group()) if m.group().startswith(("/*", "//")) else m.group(), value
    )


def fold(value: str, constants: dict[str, str], stack: tuple[str, ...] = ()) -> str:
    result = []
    for part in split_expression(_without_comments(value), "+"):
        if re.fullmatch(r'"(?:\\.|[^"\\])*"', part):
            try:
                result.append(json.loads(part))
            except ValueError:
                result.append("{unresolved}")
        elif part in constants and part not in stack and len(stack) < 20:
            result.append(fold(constants[part], constants, (*stack, part)))
        elif re.fullmatch(r"[\w.]+", part):
            result.append("{java:" + part + "}")
        elif not part:
            result.append("")
        else:
            result.append("{unresolved}")
    return "".join(result)


def annotation_paths(args: str | None, constants: dict[str, str]) -> list[str]:
    if not args:
        return [""]
    selected = ""
    for entry in split_expression(args, ","):
        named = re.match(r"(?:value|path)\s*=\s*(.*)", entry, re.S)
        if named:
            selected = named.group(1)
            break
        if not re.match(r"\w+\s*=", entry):
            selected = entry
            break
    if selected.startswith("{") and selected.endswith("}"):
        return [fold(item, constants) for item in split_expression(selected[1:-1], ",")]
    return [fold(selected, constants)]


def render_partial(template: str) -> str:
    return _TOKEN.sub("{unresolved}", template)


def resolve_constants(files: list[FileFacts]) -> None:
    candidates: dict[str, set[str]] = {}
    for facts in files:
        if facts.language != "java":
            continue
        for key, values in facts.constants.items():
            if len(values) == 1:

                def qualify(match: re.Match[str]) -> str:
                    head, dot, tail = match.group(1).partition(".")
                    qualified = (
                        facts.import_map.get(
                            head, (facts.package + "." if facts.package else "") + head
                        )
                        + dot
                        + tail
                    )
                    return "{java:" + qualified + "}"

                name = (facts.package + "." if facts.package else "") + key
                candidates.setdefault(name, set()).add(_TOKEN.sub(qualify, values[0]))
    table = {key: next(iter(values)) for key, values in candidates.items() if len(values) == 1}
    for facts in files:
        if facts.language != "java":
            continue

        owner = ""

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            head, dot, tail = key.partition(".")
            qualified = facts.import_map.get(head, head) + dot + tail
            names = [qualified, facts.package + "." + key]
            if "." not in key:
                names.extend(name[:-1] + key for name in facts.imports if name.endswith(".*"))
                for base in facts.superclasses.get(owner, []):
                    root, sep, rest = base.partition(".")
                    full = facts.import_map.get(root, root) + sep + rest
                    names.extend([full + "." + key, facts.package + "." + full + "." + key])
            values = {table[name] for name in names if name in table}
            return next(iter(values)) if len(values) == 1 else match.group()

        for route in facts.routes:
            owner = route.handler.rpartition(".")[0]
            template = route.path_template
            if not template:
                continue
            for _ in range(20):
                updated = _TOKEN.sub(replace, template)
                if updated == template:
                    break
                template = updated
            route.path = re.sub(r"/+", "/", render_partial(template))
        facts.diagnostics = [d for d in facts.diagnostics if d.category != "route-path-partial"]
        for line in sorted({r.line for r in facts.routes if "{unresolved}" in r.path}):
            facts.diagnostics.append(
                ParseDiagnostic(
                    line, "route-path-partial", "route-path-partial: unresolved annotation operand"
                )
            )


def transport_path(expression: str, constants: dict[str, str]) -> str:
    """Ignore generated placeholder substitution, retaining all concatenation.

    Arbitrary string rewriting is not a path identity; leave it partial.
    """
    masked = mask_source(expression)
    parens = delimiter_pairs(masked, "(", ")")
    spans = []
    consumed = -1
    for match in re.finditer(r"\.replace(?:All)?\s*\(", masked):
        if match.start() < consumed:
            continue
        closing = parens.get(match.end() - 1)
        if closing is None:
            return "{unresolved}"
        arguments = split_expression(expression[match.end() : closing], ",")
        if len(arguments) != 2:
            return "{unresolved}"
        pattern = fold(arguments[0], constants).replace("\\", "")
        if not re.fullmatch(r"\{[^{}]+\}", pattern):
            return "{unresolved}"
        spans.append((match.start(), closing + 1))
        consumed = closing + 1
    for start, end in reversed(spans):
        expression = expression[:start] + expression[end:]
    return fold(expression, constants)


def builder_argument(expression: str) -> str:
    """Extract fromPath only through known identity-preserving builder methods."""
    masked = mask_source(expression)
    match = re.match(r"\s*UriComponentsBuilder\.fromPath\s*\(", masked)
    if match is None:
        return "{unresolved}"
    pairs = delimiter_pairs(masked, "(", ")")
    closing = pairs.get(match.end() - 1)
    if closing is None:
        return "{unresolved}"
    argument = expression[match.end() : closing]
    cursor = closing + 1
    while masked[cursor:].strip():
        tail = re.match(r"\s*\.(?:buildAndExpand|build|encode|toUriString)\s*\(", masked[cursor:])
        if tail is None:
            return "{unresolved}"
        end = pairs.get(cursor + tail.end() - 1)
        if end is None:
            return "{unresolved}"
        cursor = end + 1
    return argument
