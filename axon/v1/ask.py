"""Natural-language ask: deterministic intents first, model second.

Every answer is synthesized only from graph rows and carries citations.
When the graph proves nothing, the answer says so.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Iterator
from itertools import chain
from typing import Any

from pydantic import BaseModel, Field

from axon.v1.ask_entities import (
    ENDPOINT_PATH,
    QUALIFIED_SERVICE,
    QUOTED_NODE_ID,
    EntityBindings,
    resolve_entities,
)
from axon.v1.graph import GraphSnapshot
from axon.v1.llm import LlmClient, validate_readonly_cypher
from axon.v1.models import EdgeKind, GraphEdge, GraphNode, NodeKind
from axon.v1.store import public_query_error


class Citation(BaseModel):
    node_id: str
    label: str
    file: str = ""
    line: int = 0
    repo_id: str = ""
    service_id: str = ""


class AnswerRow(BaseModel):
    source: Citation
    target: Citation | None = None
    via: Citation | None = None
    rule: str = ""
    confidence: str | None = None


class AskResponse(BaseModel):
    answer: str
    publication_id: str = ""
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
        node_id=node.id,
        label=_label(node),
        file=node.file,
        line=node.line_start,
        repo_id=node.repo_id,
        service_id=node.service_id,
    )


def _label(node: GraphNode) -> str:
    if node.kind == NodeKind.API_ENDPOINT:
        return (
            f"{node.props.get('method', '')} {node.props.get('path', '')} "
            f"({node.repo_id}::{node.service_id})"
        )
    if node.kind == NodeKind.SERVICE:
        return f"{node.repo_id}::{node.service_id}"
    return node.props.get("qualified") or node.name or node.id


def deprecatable(snap: GraphSnapshot, ep: GraphNode) -> AskResponse:
    callers = list({caller.id: caller for _, caller in snap.inbound_callers(ep.id)}.values())
    citations = [_cite(ep)] + [_cite(c) for c in callers[:10]]
    deprecated = ep.props.get("deprecated") == "true"
    if callers:
        names = ", ".join(sorted({_label(c) for c in callers})[:8])
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


INTENT_ENTITY = re.compile(
    "|".join(p.pattern for p in (QUOTED_NODE_ID, QUALIFIED_SERVICE, ENDPOINT_PATH))
)


def _intent_text(question: str) -> str:
    # Keep positions stable for deciding which side of "call" a service is on.
    return INTENT_ENTITY.sub(lambda match: " " * len(match.group()), question)


def _legacy_intents(
    snap: GraphSnapshot, question: str, entities: EntityBindings
) -> AskResponse | None:
    q = _intent_text(question).strip()
    ql = q.lower()
    ep = entities.endpoints[0] if len(entities.endpoints) == 1 else None
    syms = entities.symbols

    if re.search(r"\bdeprecat\w*\b|\b(remove|delete|retire)\b|safe to (remove|delete)", ql):
        if ep is not None:
            return deprecatable(snap, ep)
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
    ) or ("which apis" in ql and syms):
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
        if ep is not None:
            callers = snap.inbound_callers(ep.id)
            symbols = list({caller.id: caller for _, caller in callers}.values())
            cites = [_cite(ep)] + [_cite(c) for c in symbols[:10]]
            lines = [
                f"{_label(c)} ({e.evidence.file or c.file}:{e.evidence.line})"
                for e, c in callers[:10]
            ]
            answer = (
                f"{len(symbols)} in-graph caller(s) of {_label(ep)}: "
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
        if ep is not None:
            out = snap.outbound_endpoints(ep.id, transitive=bool(TRANSITIVE_CUE.search(q)))
            cites = [_cite(ep)] + [_cite(t) for t in out[:10]]
            services = sorted({f"{t.repo_id}::{t.service_id}" for t in out if t.service_id})
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
        if entities.services:
            by_service = snap.snapshot_index().endpoints_by_service
            eps = [
                endpoint
                for svc in entities.services
                for endpoint in by_service.get((svc.repo_id, svc.service_id), [])
            ]
        return AskResponse(
            answer=f"{len(eps)} API(s): " + ", ".join(_label(e) for e in eps[:20]) + ".",
            intent="list_apis",
            citations=[_cite(e) for e in eps[:10]],
        )
    return None


CALL_CUE = re.compile(r"\b(?:calls?|called|uses?|depends?|hits?|talks? to)\b|→|->", re.I)
TRANSITIVE_CUE = re.compile(r"\b(?:transitive(?:ly)?|indirectly|downstream)\b", re.I)
API_ONLY_CUE = re.compile(r"^\s*(?:which|what|list)\s+(?:apis|endpoints)\b", re.I)
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


def _standalone_callers(
    snap: GraphSnapshot, source: GraphNode | None, target: GraphNode | None, transitive: bool
) -> Iterator[tuple[GraphNode, list[tuple[GraphEdge, GraphNode]]]]:
    """Proven call sites with no caller API owning their code path."""
    by_caller: dict[str, list[tuple[GraphEdge, GraphNode]]] = defaultdict(list)
    for edge in snap.edges:
        if edge.kind != EdgeKind.CALLS_API:
            continue
        caller = snap.nodes.get(edge.from_id)
        dest = snap.nodes.get(edge.to_id)
        if caller is None or dest is None or caller.kind != NodeKind.SYMBOL or not caller.repo_id:
            continue
        if source and (caller.repo_id, caller.service_id) != (source.repo_id, source.service_id):
            continue
        if (
            target
            and not transitive
            and dest.kind == NodeKind.API_ENDPOINT
            and (dest.repo_id, dest.service_id) != (target.repo_id, target.service_id)
        ):
            continue
        by_caller[caller.id].append((edge, dest))
    for caller_id, calls in sorted(by_caller.items()):
        if not snap.endpoint_of_symbol(caller_id):
            yield snap.nodes[caller_id], calls


def _service_answer(
    snap: GraphSnapshot, entities: EntityBindings, question: str
) -> AskResponse | None:
    services = entities.services
    intent_text = _intent_text(question)
    cue = CALL_CUE.search(intent_text)
    if (
        not services
        or entities.endpoints
        or entities.symbols
        or (entities.explicit and not entities.endpoints)
        or not cue
    ):
        return None
    ql = intent_text.lower()
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
        position = question.lower().find(span.lower())
        if position < cue.start():
            tail = question[cue.end() :].lower().strip(" ?.!`\"'")
            if tail and tail not in {"directly", "indirectly", "transitively", "downstream"}:
                return None
        if cue.start() < position and not re.search(r"what.*does", ql):
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
    external: dict[tuple[str, str, int], tuple[GraphNode, Any]] = {}
    transitive = bool(TRANSITIVE_CUE.search(intent_text))
    api_origins = ((ep, snap.outbound_calls(ep.id)) for ep in sorted(endpoints, key=lambda n: n.id))
    standalone = (
        iter(())
        if API_ONLY_CUE.match(question)
        else _standalone_callers(snap, source, target, transitive)
    )
    for origin, calls in chain(api_origins, standalone):
        pending = deque([calls])
        visited = {origin.id}
        while pending:
            for edge, dest in pending.popleft():
                if dest.kind == NodeKind.EXTERNAL_API:
                    external[(edge.from_id, dest.id, edge.evidence.line)] = (dest, edge)
                    continue
                if transitive and dest.id not in visited:
                    visited.add(dest.id)
                    pending.append(snap.outbound_calls(dest.id))
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
                        source=_cite(origin),
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
                f"{missing} API(s) in {_label(source)} have no handler; "
                "their outbound calls are unknown."
            )
        from axon.v1.diagnose import link_diagnosis

        for node, edge in sorted(external.values(), key=lambda pair: (pair[0].id, pair[1].from_id)):
            detail = link_diagnosis(snap, edge)
            if detail["reason"] == "external-dependency":
                continue
            scope = "::".join(filter(None, [detail["target_repo"], detail["target_service"]]))
            qualifier = f" targeting {scope}" if scope else ""
            spots.append(
                f"Unresolved call{qualifier}: {node.name or node.id} "
                f"({detail['reason']}; {edge.evidence.rule})."
            )
        source_files = index.files_by_service.get((source.repo_id, source.service_id), set())
        for diagnostic in snap.source_diagnostics(source.repo_id):
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
                ) and (
                    client.props.get("target_repo") or client.repo_id,
                    client.props.get("target_service"),
                ) == (target.repo_id, target.service_id):
                    spots.append(
                        f"{_label(source)} declares a client for {_label(target)}, "
                        "but no call site resolved to it. "
                        "Review client packages and factories: in axon.yaml."
                    )
    else:
        spots.append(
            "External callers and runtime-only dispatch are not visible to static analysis."
        )
    origin_ids = {r.source.node_id for r in rows}
    standalone_ids = {sid for sid in origin_ids if snap.nodes[sid].kind == NodeKind.SYMBOL}
    api_count = len(origin_ids - standalone_ids)
    caller_count = " and ".join(
        part
        for part in (
            f"{api_count} API(s)" if api_count or not standalone_ids else "",
            f"{len(standalone_ids)} standalone function(s)" if standalone_ids else "",
        )
        if part
    )
    if source and target:
        scope = " indirectly" if transitive else ""
        summary = (
            f"{caller_count} in {_label(source)} call {_label(target)}{scope}."
            if rows
            else (
                f"No API in {_label(source)} has a resolved call to "
                f"{_label(target)} in indexed code."
            )
        )
        if ql.startswith("does ") and rows:
            summary = "Yes. " + summary
    elif source:
        counts: dict[str, set[str]] = {}
        for row in rows:
            if row.target:
                dest = snap.nodes[row.target.node_id]
                counts.setdefault(f"{dest.repo_id}::{dest.service_id}", set()).add(dest.id)
        summary = (
            f"Service {_label(source)} has resolved calls to "
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
            f"{caller_count} have resolved calls to "
            f"{_label(target) if target else ''} in indexed code."
        )
        if standalone_ids:
            owners = {
                "::".join(filter(None, (snap.nodes[sid].repo_id, snap.nodes[sid].service_id)))
                for sid in standalone_ids
            }
            summary += f" Standalone callers are indexed in {', '.join(sorted(owners))}."
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


def ask_deterministic(
    snap: GraphSnapshot, question: str, *, entities: EntityBindings | None = None
) -> AskResponse | None:
    entities = entities if entities is not None else resolve_entities(snap, question)
    response: AskResponse | None
    if entities.ambiguous:
        candidates = sorted({f"{_label(n)} [{n.id}]" for n in entities.ambiguous})
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
            else (
                _service_answer(snap, entities, question)
                or _legacy_intents(snap, question, entities)
            )
        )
        if response is None and (entities.records or entities.explicit):
            candidates = sorted({_label(n) for n in snap.by_kind(NodeKind.SERVICE)})
            response = AskResponse(
                answer="Unknown from the graph. Please specify an API path, code symbol, "
                "or source and target service. Did you mean: " + ", ".join(candidates[:20]) + "?",
                intent="unknown",
                grounded=False,
            )
    if response is not None:
        response.publication_id = snap.publication_id
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
    if response is not None:
        _add_freshness_spots(snap, response)
    return response


def _add_freshness_spots(snap: GraphSnapshot, response: AskResponse) -> None:
    # Missing retained caller inputs anywhere can leave inbound coverage incomplete.
    repos = {node.repo_id for node in snap.by_kind(NodeKind.REPO)}
    repos.update(c.repo_id for c in response.citations if c.repo_id)
    unknown = {repo for repo in repos if not snap.complete_link_inputs(repo)}
    unknown.update(
        snap.nodes[edge.from_id].repo_id
        for edge in snap.edges
        if edge.kind == EdgeKind.CALLS_API
        and edge.from_id in snap.nodes
        and edge.props.get("relink_status") == "pinned-legacy"
    )
    for repo in sorted(unknown):
        spot = (
            f"{repo}: link-inputs-missing; pinned-legacy rows and coverage "
            "do not prove fresh matching. Re-index this repository."
        )
        if spot not in response.blind_spots:
            response.blind_spots.append(spot)
            response.answer += "\n" + spot


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


async def _ask(
    snap: GraphSnapshot,
    question: str,
    llm: LlmClient | None = None,
    store: Any = None,
    engine: str = "auto",
) -> AskResponse:
    """Answer with the selected engine.

    auto          deterministic intents first, model second (default)
    deterministic graph intents only; a model is never consulted
    model         clarify ownership, then draft a graph query
    """
    if engine not in ("auto", "deterministic", "model"):
        engine = "auto"
    entities = resolve_entities(snap, question, filter_values_as_data=engine == "model")
    # Engine choice changes the answering mechanism, never repository ownership.
    # Unrelated quoted filter literals may be unresolved symbols; only missing
    # ownership scopes and ambiguous graph entities gate explicit Model queries.
    if entities.ambiguous or entities.unresolved_scopes:
        clarification = ask_deterministic(snap, question, entities=entities)
        if clarification is not None:
            return clarification
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
    bindings = entities.records
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
                    f"\nPrevious query failed to execute ({public_query_error(exc)}): {cypher}\n"
                    "Draft a corrected single read-only Cypher query."
                )
                continue
            return AskResponse(
                answer=f"Query execution failed: {public_query_error(exc)}",
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


async def ask(
    snap: GraphSnapshot,
    question: str,
    llm: LlmClient | None = None,
    store: Any = None,
    engine: str = "auto",
) -> AskResponse:
    response = await _ask(snap, question, llm=llm, store=store, engine=engine)
    # Model execution may observe a later publication than the drafting snapshot.
    # The list-returning query adapter provides no exact publication binding.
    response.publication_id = "" if response.model_used else snap.publication_id
    if response.model_used:
        response.blind_spots.append(
            "Query execution publication was not captured; publication_id is unknown."
        )
        _add_freshness_spots(snap, response)
    return response
