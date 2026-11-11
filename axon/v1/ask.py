"""Natural-language ask: deterministic intents first, model second.

Every answer is synthesized only from graph rows and carries citations.
When the graph proves nothing, the answer says so.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from axon.v1.ask_entities import EntityBindings, resolve_entities
from axon.v1.graph import GraphSnapshot
from axon.v1.llm import LlmClient, validate_readonly_cypher
from axon.v1.models import GraphNode, NodeKind


class Citation(BaseModel):
    node_id: str
    label: str
    file: str = ""
    line: int = 0


class AnswerRow(BaseModel):
    source: Citation
    target: Citation | None = None
    via: Citation | None = None
    rule: str = ""
    confidence: str | None = None


class AskResponse(BaseModel):
    answer: str
    intent: str
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    cypher: str = ""
    model_used: bool = False
    grounded: bool = True
    summary: str = ""
    rows: list[AnswerRow] = Field(default_factory=list)
    blind_spots: list[str] = Field(default_factory=list)
    bindings: list[dict[str, str]] = Field(default_factory=list)


def _cite(node: GraphNode) -> Citation:
    return Citation(
        node_id=node.id, label=node.name or node.id, file=node.file, line=node.line_start
    )


def _label(node: GraphNode) -> str:
    if node.kind == NodeKind.API_ENDPOINT:
        return f"{node.props.get('method', '')} {node.props.get('path', '')} ({node.service_id})"
    return node.props.get("qualified") or node.name or node.id


def _endpoint_from_question(snap: GraphSnapshot, q: str) -> GraphNode | None:
    entities = resolve_entities(snap, q)
    return (
        entities.endpoints[0] if len(entities.endpoints) == 1 and not entities.ambiguous else None
    )


def _symbols_from_question(snap: GraphSnapshot, q: str) -> list[GraphNode]:
    return resolve_entities(snap, q).symbols


def deprecatable(snap: GraphSnapshot, ep: GraphNode) -> AskResponse:
    callers = snap.inbound_callers(ep.id)
    citations = [_cite(ep)] + [_cite(c) for _, c in callers[:10]]
    deprecated = ep.props.get("deprecated") == "true"
    if callers:
        names = ", ".join(sorted({_label(c) for _, c in callers})[:8])
        answer = (
            f"No — {_label(ep)} still has {len(callers)} in-graph caller(s): {names}. "
            "Remove or migrate those callers first."
        )
    else:
        answer = (
            f"No callers of {_label(ep)} exist in indexed code"
            + (" and the spec already marks it deprecated" if deprecated else "")
            + ". It is a deprecation candidate, but external or runtime-only "
            "callers are not visible to static analysis — confirm with traffic "
            "logs before removing it."
        )
    return AskResponse(
        answer=answer,
        intent="deprecatable",
        citations=citations,
        evidence=[
            {"endpoint": ep.id, "inbound_callers": len(callers), "spec_deprecated": deprecated}
        ],
    )


def _legacy_intents(snap: GraphSnapshot, question: str) -> AskResponse | None:
    q = question.strip()
    ql = q.lower()

    if re.search(r"\bdeprecat\w*\b|\b(remove|delete|retire)\b|safe to (remove|delete)", ql):
        ep = _endpoint_from_question(snap, q)
        if ep is not None:
            return deprecatable(snap, ep)
        syms = _symbols_from_question(snap, q)
        if syms:
            answers = []
            cites: list[Citation] = []
            for sym in syms[:3]:
                apis = snap.endpoint_of_symbol(sym.id)
                cites.append(_cite(sym))
                cites.extend(_cite(a) for a in apis[:5])
                if apis:
                    answers.append(
                        f"{_label(sym)} is still part of {len(apis)} API(s): "
                        + ", ".join(_label(a) for a in apis[:6])
                        + ". Do not deprecate it yet."
                    )
                else:
                    answers.append(
                        f"No indexed API includes {_label(sym)}. It is a "
                        "deprecation candidate; external/runtime uses are "
                        "not visible statically."
                    )
            return AskResponse(answer=" ".join(answers), intent="deprecatable", citations=cites)

    if re.search(
        r"(apis|endpoints).*(function|method|contain|include|part of)"
        r"|(function|method).*(part of|used|apis)",
        ql,
    ) or ("which apis" in ql and _symbols_from_question(snap, q)):
        syms = _symbols_from_question(snap, q)
        if syms:
            parts, cites = [], []
            for sym in syms[:3]:
                apis = snap.endpoint_of_symbol(sym.id)
                cites.append(_cite(sym))
                cites.extend(_cite(a) for a in apis[:8])
                parts.append(
                    f"{_label(sym)} is part of {len(apis)} API(s): "
                    + (", ".join(_label(a) for a in apis[:8]) or "none in indexed code")
                    + "."
                )
            return AskResponse(answer=" ".join(parts), intent="function_apis", citations=cites)

    if re.search(r"\bwho calls\b|\bcallers of\b|\bcalled by\b|\bconsumers of\b", ql):
        ep = _endpoint_from_question(snap, q)
        if ep is not None:
            callers = snap.inbound_callers(ep.id)
            cites = [_cite(ep)] + [_cite(c) for _, c in callers[:10]]
            lines = [f"{_label(c)} ({c.file}:{e.evidence.line})" for e, c in callers[:10]]
            answer = (
                f"{len(callers)} in-graph caller(s) of {_label(ep)}: "
                + ("; ".join(lines) or "none")
                + "."
            )
            if ep.props.get("framework") == "django":
                answer += (
                    " Middleware and runtime-only framework hooks are outside "
                    "this endpoint's static closure."
                )
            return AskResponse(answer=answer, intent="callers", citations=cites)

    if re.search(
        r"what (does|services|apis)|calls?\b.*\bmake|outbound|dependencies of"
        r"|downstream",
        ql,
    ):
        ep = _endpoint_from_question(snap, q)
        if ep is not None:
            out = snap.outbound_endpoints(ep.id, transitive=("transitive" in ql or "all" in ql))
            cites = [_cite(ep)] + [_cite(t) for t in out[:10]]
            services = sorted({t.service_id for t in out if t.service_id})
            answer = (
                f"{_label(ep)} calls {len(out)} API(s)"
                + (f" across services: {', '.join(services)}" if services else "")
                + ": "
                + (", ".join(_label(t) for t in out[:10]) or "none in indexed code")
                + "."
            )
            if ep.props.get("framework") == "django":
                answer += (
                    " Middleware and runtime-only framework hooks are outside "
                    "this endpoint's static closure."
                )
            return AskResponse(answer=answer, intent="outbound", citations=cites)

    if not CALL_CUE.search(q) and re.search(
        r"\blist\b.*\b(apis|endpoints|services)\b|what (apis|services)", ql
    ):
        if "service" in ql and "api" not in ql:
            svcs = snap.by_kind(NodeKind.SERVICE)
            return AskResponse(
                answer=f"{len(svcs)} service(s): "
                + ", ".join(f"{s.service_id} ({s.repo_id})" for s in svcs[:20])
                + ".",
                intent="list_services",
                citations=[_cite(s) for s in svcs[:10]],
            )
        eps = snap.by_kind(NodeKind.API_ENDPOINT)
        bindings = resolve_entities(snap, q)
        if bindings.services:
            svc = bindings.services[0]
            eps = snap.snapshot_index().endpoints_by_service.get((svc.repo_id, svc.service_id), [])
        return AskResponse(
            answer=f"{len(eps)} API(s): " + ", ".join(_label(e) for e in eps[:20]) + ".",
            intent="list_apis",
            citations=[_cite(e) for e in eps[:10]],
        )
    return None


CALL_CUE = re.compile(r"\b(?:calls?|called|uses?|depends?|hits?|talks? to)\b|→|->", re.I)
DETERMINISTIC_INTENTS = frozenset(
    {
        "deprecatable",
        "function_apis",
        "callers",
        "outbound",
        "service_outbound",
        "service_inbound",
        "service_to_service",
        "list_apis",
        "list_services",
        "ambiguous",
        "unknown",
    }
)


def _service_answer(
    snap: GraphSnapshot, entities: EntityBindings, question: str
) -> AskResponse | None:
    services = entities.services
    if (
        not services
        or entities.endpoints
        or entities.symbols
        or (entities.explicit and not entities.endpoints)
        or not CALL_CUE.search(question)
    ):
        return None
    ql = question.lower()
    target: GraphNode | None = None
    source: GraphNode | None = None
    reverse = bool(re.search(r"called by", ql))
    if len(services) == 2:
        source, target = services[::-1] if reverse else services
        if not reverse:
            positioned = [
                service
                for service in services
                if any(
                    re.search(
                        r"\b(?:in|of|from)\s+(?:the\s+)?(?:service\s+)?"
                        + re.escape(binding["span"])
                        + r"\b",
                        question,
                        re.I,
                    )
                    for binding in entities.records
                    if binding["node_id"] == service.id
                )
            ]
            if len(positioned) == 1:
                source = positioned[0]
                target = next(service for service in services if service.id != source.id)
        intent = "service_to_service"
    elif len(services) == 1:
        # The bound service is an object after 'call/use', a subject before it.
        span = next(b["span"] for b in entities.records if b["kind"] == "service")
        cue_match = CALL_CUE.search(ql)
        if cue_match and ql.find(span.lower()) < cue_match.start():
            tail = ql[cue_match.end() :].strip(" ?.!`\"'")
            if tail and tail not in {"directly", "indirectly", "transitively"}:
                return None
        position = ql.find(span.lower())
        cue = CALL_CUE.search(ql)
        if cue and cue.start() < position and not re.search(r"what.*does", ql):
            target = services[0]
            intent = "service_inbound"
        else:
            source = services[0]
            intent = "service_outbound"
    else:
        return None
    for binding in entities.records:
        if binding["kind"] == "service":
            binding["role"] = "source" if source and binding["node_id"] == source.id else "target"
    index = snap.snapshot_index()
    endpoints = (
        index.endpoints_by_service.get((source.repo_id, source.service_id), [])
        if source
        else snap.by_kind(NodeKind.API_ENDPOINT)
    )
    rows: list[AnswerRow] = []
    spots: list[str] = []
    external: dict[str, tuple[GraphNode, str]] = {}
    transitive = bool(re.search(r"\b(transitive|indirectly)\b", ql))
    for ep in sorted(endpoints, key=lambda n: n.id):
        pending = [ep]
        visited = {ep.id}
        while pending:
            current = pending.pop(0)
            for edge, dest in snap.outbound_calls(current.id):
                if dest.kind == NodeKind.EXTERNAL_API:
                    external[dest.id] = (dest, edge.evidence.rule)
                    continue
                if transitive and dest.id not in visited:
                    visited.add(dest.id)
                    pending.append(dest)
                if target and (dest.repo_id, dest.service_id) != (
                    target.repo_id,
                    target.service_id,
                ):
                    continue
                caller = snap.nodes.get(edge.from_id)
                via = _cite(caller) if caller else None
                if via:
                    via.file = edge.evidence.file or via.file
                    via.line = edge.evidence.line or via.line
                rows.append(
                    AnswerRow(
                        source=_cite(ep),
                        target=_cite(dest),
                        via=via,
                        rule=edge.evidence.rule,
                        confidence=edge.confidence.value,
                    )
                )
    rows = list(
        {
            (
                r.source.node_id,
                r.target.node_id if r.target else "",
                r.via.node_id if r.via else "",
                r.via.line if r.via else 0,
            ): r
            for r in rows
        }.values()
    )
    rows.sort(
        key=lambda r: (
            (r.target.node_id if r.target else "") if reverse else r.source.node_id,
            r.source.node_id,
            r.target.node_id if r.target else "",
        )
    )
    if source:
        missing = sum(not snap.handlers_of(ep.id) for ep in endpoints)
        if missing:
            spots.append(
                f"{missing} API(s) in {source.service_id} have no handler; "
                "their outbound calls are unknown."
            )
        for node, rule in sorted(external.values(), key=lambda pair: pair[0].id):
            hay = " ".join([node.name, *node.props.values()]).lower()
            possible = target and any(
                alias.lower() in hay
                for alias in (target.service_id, target.name)
                if len(alias) >= 3
            )
            qualifier = f" may target {target.service_id}" if possible and target else ""
            spots.append(f"Unresolved call{qualifier}: {node.name or node.id} ({rule}).")
        source_files = index.files_by_service.get((source.repo_id, source.service_id), set())
        for diagnostic in snap.diagnostics:
            file = diagnostic.split(":", 1)[0]
            if file in source_files and any(
                term in diagnostic.lower()
                for term in ("untyped client", "factory package", "django-settings:")
            ):
                spots.append("Unlinked client evidence: " + diagnostic)
        if target and not rows:
            for client in snap.by_kind(NodeKind.API_CLIENT):
                if (client.repo_id, client.service_id) == (
                    source.repo_id,
                    source.service_id,
                ) and client.props.get("target_service") == target.service_id:
                    spots.append(
                        f"{source.service_id} declares a client for {target.service_id}, "
                        "but no call site resolved to it. "
                        "Review client packages and factories: in axon.yaml."
                    )
    else:
        spots.append(
            "External callers and runtime-only dispatch are not visible to static analysis."
        )
    count = len({r.source.node_id for r in rows})
    if source and target:
        scope = " indirectly" if transitive else ""
        summary = (
            f"{count} API(s) in {source.service_id} call {target.service_id}{scope}."
            if rows
            else (
                f"No API in {source.service_id} has a resolved call to "
                f"{target.service_id} in indexed code."
            )
        )
        if ql.startswith("does ") and rows:
            summary = "Yes. " + summary
    elif source:
        counts: dict[str, set[str]] = {}
        for row in rows:
            if row.target:
                dest = snap.nodes[row.target.node_id]
                counts.setdefault(dest.service_id, set()).add(dest.id)
        summary = (
            f"Service {source.service_id} has resolved calls to "
            f"{sum(map(len, counts.values()))} API(s)"
            + (
                ": " + ", ".join(f"{sid} ({len(ids)})" for sid, ids in sorted(counts.items()))
                if counts
                else " in indexed code"
            )
            + "."
        )
    else:
        summary = (
            f"{count} API(s) have resolved calls to "
            f"{target.service_id if target else ''} in indexed code."
        )
    lines = [
        f"{r.source.label} → {r.target.label if r.target else ''}"
        + (f" ({r.via.file}:{r.via.line}; {r.rule})" if r.via else "")
        for r in rows
    ]
    return AskResponse(
        answer="\n".join([summary, *lines, *spots]),
        summary=summary,
        intent=intent,
        rows=rows,
        blind_spots=spots,
        citations=list(
            {c.node_id: c for r in rows for c in (r.source, r.target, r.via) if c}.values()
        ),
    )


def ask_deterministic(snap: GraphSnapshot, question: str) -> AskResponse | None:
    entities = resolve_entities(snap, question)
    response: AskResponse | None
    if entities.ambiguous:
        candidates = sorted(
            {f"{n.name or n.service_id or n.id} [{n.id}]" for n in entities.ambiguous}
        )
        response = AskResponse(
            answer="Ambiguous entity. Please choose: " + "; ".join(candidates),
            intent="ambiguous",
            grounded=False,
            citations=[_cite(n) for n in entities.ambiguous],
        )
    else:
        response = (
            None
            if entities.unresolved
            else (_service_answer(snap, entities, question) or _legacy_intents(snap, question))
        )
        if response is None and (entities.records or entities.explicit):
            candidates = sorted({n.service_id for n in snap.by_kind(NodeKind.SERVICE)})
            response = AskResponse(
                answer="Unknown from the graph. Please specify an API path, code symbol, "
                "or source and target service. Did you mean: " + ", ".join(candidates[:20]) + "?",
                intent="unknown",
                grounded=False,
            )
    if response is not None:
        response.bindings = entities.records
        response.evidence.extend(entities.records)
        response.summary = (
            response.summary or re.split(r"(?<=[.!?])\s+", response.answer, maxsplit=1)[0]
        )
        if response.intent == "function_apis":
            response.summary = (
                "; ".join(
                    f"{_label(sym)}: {len(snap.endpoint_of_symbol(sym.id))} API(s)"
                    for sym in entities.symbols
                )
                + "."
            )
            response.rows = [
                AnswerRow(source=_cite(ep), via=_cite(sym))
                for sym in entities.symbols
                for ep in snap.endpoint_of_symbol(sym.id)
            ]
    return response


MAX_MODEL_ATTEMPTS = 3  # initial draft + up to two repairs


def _schema_card(snap: GraphSnapshot) -> str:
    """Graph-derived schema summary for the drafting prompt.

    Counts and relationship triples come from the actual snapshot, so
    the model sees which labels, relationship types, and directions
    exist instead of guessing from the static hint in llm.py.
    """
    kind_counts: dict[str, int] = {}
    props_by_kind: dict[str, set[str]] = {}
    for node in snap.nodes.values():
        kind = node.kind.value
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        props_by_kind.setdefault(kind, set()).update(node.props.keys())
    triples: dict[str, int] = {}
    for edge in snap.edges:
        src = snap.nodes.get(edge.from_id)
        dst = snap.nodes.get(edge.to_id)
        if src is None or dst is None:
            continue
        key = f"({src.kind.value})-[:{edge.kind.value}]->({dst.kind.value})"
        triples[key] = triples.get(key, 0) + 1
    lines = ["Graph schema (from the indexed snapshot):"]
    for kind in sorted(kind_counts):
        props = ", ".join(sorted(props_by_kind[kind] - {"id"})[:8])
        lines.append(f"  {kind} x{kind_counts[kind]}" + (f" props: {props}" if props else ""))
    for triple in sorted(triples):
        lines.append(f"  {triple} x{triples[triple]}")
    lines.append("Return node ids (AS id) or endpoint method/path where possible.")
    return "\n".join(lines)


async def ask(
    snap: GraphSnapshot,
    question: str,
    llm: LlmClient | None = None,
    store: Any = None,
    engine: str = "auto",
) -> AskResponse:
    """Answer with the selected engine.

    auto          deterministic intents first, model second (default)
    deterministic graph intents only; a model is never consulted
    model         skip the deterministic layer, draft a graph query
    """
    if engine not in ("auto", "deterministic", "model"):
        engine = "auto"
    if engine != "model":
        det = ask_deterministic(snap, question)
        if det is not None:
            return det
        if engine == "deterministic":
            return AskResponse(
                answer="Unknown from the graph. No deterministic intent matched, "
                "and the Deterministic engine never consults a model. "
                "Try asking about callers, outbound APIs, deprecation, or "
                "which APIs include a named function — or switch the engine to Auto.",
                intent="unknown",
                grounded=True,
            )
    if llm is None:
        return AskResponse(
            answer="Unknown from the graph. No deterministic intent matched "
            "and no language model is configured (LLM_PROVIDER=none). "
            "Try asking about callers, outbound APIs, deprecation, or "
            "which APIs include a named function."
            if engine != "model"
            else "The Model engine is selected, but no language model is "
            "configured (LLM_PROVIDER=none), so no query can be drafted. "
            "Switch to Auto for deterministic answers.",
            intent="unknown",
            grounded=True,
        )
    bindings = resolve_entities(snap, question).records
    prompt = question + (
        "\nResolved graph entities (identifiers only): " + str(bindings) if bindings else ""
    )
    prompt += "\n" + _schema_card(snap)
    # Draft, then repair rejected or non-executable drafts with the
    # validator/executor feedback. An empty result is a valid answer
    # and is never repaired away.
    feedback = ""
    cypher = ""
    rows: list[dict[str, Any]] = []
    for attempt in range(MAX_MODEL_ATTEMPTS):
        try:
            cypher = await llm.draft_cypher(prompt + feedback)
        except Exception as exc:
            return AskResponse(answer=f"Model unavailable: {exc}", intent="error", model_used=True)
        ok, why = validate_readonly_cypher(cypher)
        if not ok:
            if attempt + 1 < MAX_MODEL_ATTEMPTS:
                feedback = (
                    f"\nPrevious query was rejected ({why}): {cypher}\n"
                    "Draft a corrected single read-only Cypher query."
                )
                continue
            return AskResponse(
                answer=f"The model proposed a query that was rejected ({why}). "
                "No answer was generated.",
                intent="cypher_rejected",
                cypher=cypher,
                model_used=True,
            )
        if store is None:
            return AskResponse(
                answer="A read-only query was drafted but no Neo4j store is attached "
                "to execute it.",
                intent="cypher_drafted",
                cypher=cypher,
                model_used=True,
            )
        try:
            rows = store.run_cypher(cypher)
            break
        except Exception as exc:
            if attempt + 1 < MAX_MODEL_ATTEMPTS:
                feedback = (
                    f"\nPrevious query failed to execute ({str(exc)[:300]}): {cypher}\n"
                    "Draft a corrected single read-only Cypher query."
                )
                continue
            return AskResponse(
                answer=f"Query execution failed: {exc}",
                intent="cypher_error",
                cypher=cypher,
                model_used=True,
            )
    if not rows:
        return AskResponse(
            answer="Unknown from the graph: the generated query returned no rows.",
            intent="cypher",
            cypher=cypher,
            model_used=True,
        )
    preview = "; ".join(", ".join(f"{k}={v}" for k, v in list(r.items())[:4]) for r in rows[:8])
    return AskResponse(
        answer=f"The graph returned {len(rows)} row(s): {preview}.",
        intent="cypher",
        cypher=cypher,
        model_used=True,
        evidence=rows[:20],
    )
