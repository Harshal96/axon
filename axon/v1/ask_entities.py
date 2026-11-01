"""Question-local entity resolution; dictionary words are never symbol evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from axon.v1.graph import GraphSnapshot, service_token
from axon.v1.models import GraphNode

STOP_WORDS = frozenset(
    "api apis service services svc endpoint endpoints call calls use uses "
    "function method class symbol".split()
)
TOKEN = re.compile(r"[\w]+(?:[-_][\w]+)*")


@dataclass
class EntityBindings:
    services: list[GraphNode] = field(default_factory=list)
    endpoints: list[GraphNode] = field(default_factory=list)
    symbols: list[GraphNode] = field(default_factory=list)
    records: list[dict[str, str]] = field(default_factory=list)
    ambiguous: list[GraphNode] = field(default_factory=list)
    explicit: bool = False
    unresolved: list[str] = field(default_factory=list)


def resolve_entities(snap: GraphSnapshot, question: str) -> EntityBindings:
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
                    "span": span,
                    "rule": rule,
                }
            )

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

    return result
