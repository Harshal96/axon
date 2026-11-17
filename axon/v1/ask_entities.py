"""Question-local entity resolution; dictionary words are never symbol evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from axon.v1.graph import GraphSnapshot, service_token
from axon.v1.models import GraphNode, NodeKind

STOP_WORDS = frozenset(
    "api apis service services svc endpoint endpoints call calls use uses "
    "function method class symbol".split()
)
TOKEN = re.compile(r"[\w]+(?:[-_][\w]+)*")
FILTER_VALUE = re.compile(
    r"\b(?:where|whose|with|and|or)\s+[\w.]+\s*"
    r"(?:==|!=|<=|>=|=|<|>|\b(?:equals|is(?:\s+not)?|contains|matches)\b)\s*"
    r"(?P<value>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")",
    re.I,
)


@dataclass
class EntityBindings:
    services: list[GraphNode] = field(default_factory=list)
    endpoints: list[GraphNode] = field(default_factory=list)
    symbols: list[GraphNode] = field(default_factory=list)
    records: list[dict[str, str]] = field(default_factory=list)
    ambiguous: list[GraphNode] = field(default_factory=list)
    explicit: bool = False
    unresolved: list[str] = field(default_factory=list)
    unresolved_scopes: list[str] = field(default_factory=list)


def resolve_entities(
    snap: GraphSnapshot, question: str, *, filter_values_as_data: bool = False
) -> EntityBindings:
    if filter_values_as_data:
        # Preserve offsets and the original drafting prompt while keeping quoted
        # comparison values out of ownership/entity binding. Quoted call targets
        # without an explicit property comparison remain ordinary entity evidence.
        question = FILTER_VALUE.sub(
            lambda match: (
                match.group()[: match.start("value") - match.start()]
                + " " * len(match.group("value"))
            ),
            question,
        )
    index = snap.snapshot_index()
    result = EntityBindings()
    tokens = list(TOKEN.finditer(question))
    consumed: set[int] = set()
    paths = list(re.finditer(r"/[\w/{}:.*-]+", question))

    def bind(kind: str, node: GraphNode, span: str, rule: str) -> None:
        nodes = getattr(result, kind + "s")
        if node.id not in {n.id for n in nodes}:
            nodes.append(node)
            result.records.append(
                {
                    "kind": kind,
                    "id": node.service_id if kind == "service" else node.id,
                    "node_id": node.id,
                    "repo_id": node.repo_id,
                    "service_id": node.service_id,
                    "span": span,
                    "rule": rule,
                }
            )

    # Resolve explicit repository scopes before path parsing (repo IDs may contain /).
    qualified = list(re.finditer(r"[\w./-]+::[\w-]+", question))
    paths = [p for p in paths if not any(q.start() <= p.start() < q.end() for q in qualified)]
    for match in qualified:
        result.explicit = True
        hits = [
            node
            for node in snap.by_kind(NodeKind.SERVICE)
            if f"{node.repo_id}::{node.service_id}".lower() == match.group().lower()
        ]
        consumed.update(
            i for i, token in enumerate(tokens) if match.start() <= token.start() < match.end()
        )
        if len(hits) == 1:
            bind("service", hits[0], match.group(), "service:qualified")
        elif hits:
            result.ambiguous.extend(hits)
        else:
            result.unresolved.append(match.group())
            result.unresolved_scopes.append(match.group())
    # Service scope alone is not an explicit endpoint/symbol request.
    if qualified and not result.unresolved:
        result.explicit = False

    # Longest phrase at a position wins; equal aliases remain ambiguous.
    for i, token in enumerate(tokens):
        if i in consumed or any(match.start() <= token.start() < match.end() for match in paths):
            continue
        if (token.start() > 0 and question[token.start() - 1] == ".") or (
            token.end() < len(question) and question[token.end()] == "."
        ):
            continue
        for j in range(min(len(tokens), i + 8), i, -1):
            span = question[token.start() : tokens[j - 1].end()]
            hits = index.services_by_token.get(service_token(span), [])
            if not hits:
                continue
            suffix = question[tokens[j - 1].end() :]
            quoted = token.start() > 0 and question[token.start() - 1] in "`\"'"
            if len(service_token(span)) < 3 and not (
                quoted or re.match(r"\s+(?:service|svc)\b", suffix, re.I)
            ):
                continue
            consumed.update(range(i, j))
            if len(hits) > 1:
                result.ambiguous.extend(hits)
            else:
                bind("service", hits[0], span, "service:exact-token")
            break

    def scoped(nodes: list[GraphNode], shared: bool = False) -> list[GraphNode]:
        if not result.services:
            return nodes
        return [
            node
            for node in nodes
            if any(
                node.repo_id == svc.repo_id
                and (node.service_id == svc.service_id or (shared and not node.service_id))
                for svc in result.services
            )
        ]

    for match in paths:
        result.explicit = True
        path = match.group().rstrip(".")
        hits = index.endpoints_by_name.get(path.lower(), [])
        verb = re.search(
            r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*$", question[: match.start()], re.I
        )
        if verb:
            hits = [n for n in hits if n.props.get("method", "").lower() == verb.group(1).lower()]
        hits = scoped(hits)
        if len(hits) == 1:
            bind("endpoint", hits[0], match.group(), "endpoint:explicit-path")
        elif hits:
            result.ambiguous.extend(hits)
        else:
            result.unresolved.append(match.group())

    candidates = list(
        re.finditer(
            r"[`\"']([^`\"']+)[`\"']|\b(?:function|method|class|symbol)\s+([\w.]+)|\b([A-Za-z_]\w*(?:\.\w+)*)",
            question,
        )
    )
    for match in candidates:
        value = next(v for v in match.groups() if v is not None)
        if value.lower() in STOP_WORDS and match.group(1) is None and match.group(2) is None:
            continue
        start = next(match.start(k) for k in range(1, 4) if match.group(k) is not None)
        if any(tokens[i].start() <= start < tokens[i].end() for i in consumed):
            continue
        if any(m.start() <= start < m.end() for m in paths):
            continue
        # Exact operation IDs may be ordinary words; symbol names may not.
        eps = [
            n
            for n in index.endpoints_by_name.get(value.lower(), [])
            if n.props.get("operation_id", "").lower() == value.lower()
        ]
        if eps:
            eps = scoped(eps)
            result.explicit = True
            if len(eps) == 1:
                bind("endpoint", eps[0], value, "endpoint:operation-id")
            elif eps:
                result.ambiguous.extend(eps)
            else:
                result.unresolved.append(value)
            continue
        shaped = (
            match.group(1) is not None
            or match.group(2) is not None
            or "_" in value
            or "." in value
            or bool(re.search(r"[a-z][A-Z]", value))
        )
        if not shaped:
            continue
        result.explicit = True
        hits = scoped(index.symbols_by_name.get(value.lower(), []), shared=True)
        if len(hits) == 1:
            bind("symbol", hits[0], value, "symbol:code-shaped")
        elif hits:
            result.ambiguous.extend(hits)
        else:
            result.unresolved.append(value)
    # A standalone `service X` qualifier is ownership evidence even without a
    # preceding in/from/of (for example `who calls service X?`).
    for qualifier in re.finditer(r"\bservice\s+[`\"']?([\w-]+)", question, re.I):
        value = qualifier.group(1)
        if re.search(
            r"\b(?:each|every|any|per|which|what|all)\s+$", question[: qualifier.start()], re.I
        ):
            continue
        if value.lower() in STOP_WORDS | {"in", "from", "of", "to", "and", "or", "the"}:
            continue
        if any(tokens[i].start() <= qualifier.start(1) < tokens[i].end() for i in consumed):
            continue
        hits = index.services_by_token.get(service_token(value), [])
        if len(hits) == 1:
            bind("service", hits[0], value, "service:explicit")
        elif hits:
            result.ambiguous.extend(hits)
        else:
            result.explicit = True
            result.unresolved.append(value)
            result.unresolved_scopes.append(value)
    # Explicit ownership qualifiers must not silently become an unfiltered list.
    for qualifier in re.finditer(
        r"\b(?:in|from|of)\s+(?:the\s+)?(?:service\s+)?([\w-]+)", question, re.I
    ):
        if qualifier.group(1).lower() in STOP_WORDS | {
            "get",
            "post",
            "put",
            "patch",
            "delete",
            "head",
            "options",
        }:
            continue
        if any(binding["span"].lower() == qualifier.group(1).lower() for binding in result.records):
            continue
        if not any(tokens[i].start() <= qualifier.start(1) < tokens[i].end() for i in consumed):
            result.explicit = True
            result.unresolved.append(qualifier.group(1))
            # A bare preposition can refer to free-form graph concepts (e.g.
            # "reachable within three calls from checkout"), not service scope.
            prefix = question[: qualifier.start(1)]
            if re.search(r"\bservice\s+$", prefix, re.I) or re.search(
                r"\b(?:apis|endpoints|services|symbols|functions|clients)\s+"
                r"(?:in|from|of)\s+(?:the\s+)?$",
                prefix,
                re.I,
            ):
                result.unresolved_scopes.append(qualifier.group(1))

    result.services.sort(
        key=lambda node: min(
            question.lower().find(binding["span"].lower())
            for binding in result.records
            if binding["node_id"] == node.id
        )
    )
    return result
