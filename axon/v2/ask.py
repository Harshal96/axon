"""Natural-language ask: deterministic intents first, model second.

Every answer is synthesized only from graph rows and carries citations.
When the graph proves nothing, the answer says so.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from axon.v2.graph import GraphSnapshot
from axon.v2.llm import LlmClient, validate_readonly_cypher
from axon.v2.models import GraphNode, NodeKind


class Citation(BaseModel):
    node_id: str
    label: str
    file: str = ""
    line: int = 0


class AskResponse(BaseModel):
    answer: str
    intent: str
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    cypher: str = ""
    model_used: bool = False
    grounded: bool = True


def _cite(node: GraphNode) -> Citation:
    return Citation(
        node_id=node.id, label=node.name or node.id, file=node.file, line=node.line_start
    )


def _label(node: GraphNode) -> str:
    if node.kind == NodeKind.API_ENDPOINT:
        return f"{node.props.get('method', '')} {node.props.get('path', '')} ({node.service_id})"
    return node.props.get("qualified") or node.name or node.id


def _endpoint_from_question(snap: GraphSnapshot, q: str) -> GraphNode | None:
    ql = q.lower()
    path_m = re.search(r"(/[a-z0-9_/{}\-\.]+)", ql)
    if path_m:
        hits = snap.find_endpoints(path_m.group(1))
        if hits:
            return hits[0]
    # Longest operation-id / name mention wins.
    best: GraphNode | None = None
    best_len = 0
    for ep in snap.by_kind(NodeKind.API_ENDPOINT):
        for key in (ep.props.get("operation_id", ""), ep.props.get("path", "")):
            k = (key or "").lower()
            if k and k in ql and len(k) > best_len:
                best, best_len = ep, len(k)
    if best:
        return best
    hits = snap.find_endpoints(q)
    return hits[0] if hits else None


def _symbols_from_question(snap: GraphSnapshot, q: str) -> list[GraphNode]:
    ql = q.lower()
    hits = []
    for sym in snap.by_kind(NodeKind.SYMBOL):
        names = {sym.name.lower(), sym.props.get("qualified", "").lower()}
        if any(n and re.search(rf"\b{re.escape(n)}\b", ql) for n in names):
            hits.append(sym)
    return hits


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


def ask_deterministic(snap: GraphSnapshot, question: str) -> AskResponse | None:
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
            return AskResponse(answer=answer, intent="outbound", citations=cites)
        # "what services does service X call"
        for svc in snap.by_kind(NodeKind.SERVICE):
            if svc.service_id and svc.service_id.lower() in ql:
                targets: dict[str, GraphNode] = {}
                for ep2 in snap.by_kind(NodeKind.API_ENDPOINT):
                    if ep2.service_id == svc.service_id and ep2.repo_id == svc.repo_id:
                        for t in snap.outbound_endpoints(ep2.id):
                            targets[t.id] = t
                services = sorted({t.service_id for t in targets.values() if t.service_id})
                answer = (
                    f"Service {svc.service_id} calls {len(targets)} API(s) in "
                    f"services: {', '.join(services) or 'none'}."
                )
                return AskResponse(
                    answer=answer,
                    intent="service_outbound",
                    citations=[_cite(svc)] + [_cite(t) for t in list(targets.values())[:10]],
                )

    if re.search(r"\blist\b.*\b(apis|endpoints|services)\b|what (apis|services)", ql):
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
        for svc in snap.by_kind(NodeKind.SERVICE):
            if svc.service_id and svc.service_id.lower() in ql:
                eps = [e for e in eps if e.service_id == svc.service_id]
                break
        return AskResponse(
            answer=f"{len(eps)} API(s): " + ", ".join(_label(e) for e in eps[:20]) + ".",
            intent="list_apis",
            citations=[_cite(e) for e in eps[:10]],
        )
    return None


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
    try:
        cypher = await llm.draft_cypher(question)
    except Exception as exc:
        return AskResponse(answer=f"Model unavailable: {exc}", intent="error", model_used=True)
    ok, why = validate_readonly_cypher(cypher)
    if not ok:
        return AskResponse(
            answer=f"The model proposed a query that was rejected ({why}). "
            "No answer was generated.",
            intent="cypher_rejected",
            cypher=cypher,
            model_used=True,
        )
    if store is None:
        return AskResponse(
            answer="A read-only query was drafted but no Neo4j store is attached to execute it.",
            intent="cypher_drafted",
            cypher=cypher,
            model_used=True,
        )
    try:
        rows = store.run_cypher(cypher)
    except Exception as exc:
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
