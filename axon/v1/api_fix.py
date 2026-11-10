"""Per-API corrections: validated axon.yaml edits for one endpoint.

Corrections reuse the existing manual-correction shapes (handlers, calls,
suppress) and ConfigEditor's preview/apply, so they survive a full re-index.
Nothing here calls a model to write the graph; an optional prompt only drafts
the same structured correction for review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from axon.v1.configedit import ConfigEditor, EditPlan
from axon.v1.facts import normalize_path
from axon.v1.graph import GraphSnapshot
from axon.v1.models import EdgeKind, NodeKind, endpoint_node_id, external_node_id


class DeclaredCallInput(BaseModel):
    to_service: str = Field(min_length=1)
    to_method: str = "GET"
    to_path: str = Field(min_length=1)
    to_repo: str = ""
    evidence_file: str = Field(min_length=1)
    evidence_line: int = Field(ge=1)
    evidence_text: str = Field(min_length=1)


class SuppressInput(BaseModel):
    to_service: str = ""
    from_client: str = ""
    reason: str = Field(min_length=1)


class CorrectionRequest(BaseModel):
    handler: str = ""
    add_calls: list[DeclaredCallInput] = Field(default_factory=list)
    suppress: list[SuppressInput] = Field(default_factory=list)
    prompt: str = ""

    def has_ops(self) -> bool:
        return bool(self.handler.strip() or self.add_calls or self.suppress)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48].strip("-")
    return slug or "api"


def _unique_id(existing: set[str], base: str) -> str:
    candidate, n = base, 2
    while candidate in existing:
        candidate = f"{base}-{n}"
        n += 1
    existing.add(candidate)
    return candidate


def resolve_handler(snap: GraphSnapshot, endpoint: Any, ref: str) -> Any:
    """Resolve a handler reference to a symbol in the endpoint's service."""
    ref = ref.strip()
    candidates = [
        n
        for n in snap.by_kind(NodeKind.SYMBOL)
        if n.repo_id == endpoint.repo_id
        and n.service_id == endpoint.service_id
        and (n.id == ref or n.props.get("qualified", "") == ref or n.name == ref)
    ]
    if not candidates:
        raise ValueError(f"handler not found in service {endpoint.service_id}: {ref}")
    qualified = {n.props.get("qualified", "") or n.name for n in candidates}
    if len(qualified) > 1:
        raise ValueError(f"ambiguous handler {ref!r}; use the full qualified name")
    return candidates[0]


def _validate_evidence(root: Path, call: DeclaredCallInput) -> None:
    path = (root / call.evidence_file).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"evidence file not found in repo: {call.evidence_file}")
    lines = path.read_text(errors="replace").splitlines()
    if call.evidence_line > max(1, len(lines)):
        raise ValueError(
            f"evidence line {call.evidence_line} beyond {call.evidence_file} ({len(lines)} lines)"
        )


def build_plan(
    root: Path, snap: GraphSnapshot, endpoint_id: str, body: CorrectionRequest
) -> tuple[ConfigEditor, EditPlan, dict[str, Any]]:
    endpoint = snap.nodes.get(endpoint_id)
    if endpoint is None or endpoint.kind != NodeKind.API_ENDPOINT:
        raise ValueError("endpoint not found")
    if not body.has_ops():
        raise ValueError("empty correction: set handler, add_calls, or suppress")
    editor = ConfigEditor(root)
    service = next((s for s in editor.sm.services if s.id == endpoint.service_id), None)
    if service is None:
        raise ValueError(f"service not in axon.yaml: {endpoint.service_id}")
    api_label = f"{endpoint.props.get('method', '')} {endpoint.props.get('path', '')}".strip()
    base = _slug(api_label)
    effects: list[dict[str, Any]] = []
    warnings: list[str] = []

    if body.handler.strip():
        symbol = resolve_handler(snap, endpoint, body.handler)
        qualified = symbol.props.get("qualified", "") or symbol.name
        entry_id = _unique_id({h.id for h in service.handlers}, f"fix-{base}-handler")
        editor.edit(
            "add",
            f"handler:{endpoint.service_id}/{entry_id}",
            {"id": entry_id, "api": api_label, "handler": qualified},
        )
        effects.append(
            {"kind": "HANDLES", "to": endpoint_id, "handler": qualified, "confidence": "MANUAL"}
        )

    call_ids = {c.id for c in service.calls}
    for call in body.add_calls:
        _validate_evidence(root, call)
        path = normalize_path(call.to_path.strip())
        entry_id = _unique_id(call_ids, f"fix-{base}-call")
        target: dict[str, Any] = {
            "service": call.to_service,
            "method": call.to_method.upper(),
            "path": path,
        }
        if call.to_repo:
            target["repo"] = call.to_repo
        editor.edit(
            "add",
            f"call:{endpoint.service_id}/{entry_id}",
            {
                "id": entry_id,
                "from_api": api_label,
                "to": target,
                "evidence": {
                    "file": call.evidence_file,
                    "line": call.evidence_line,
                    "text": call.evidence_text,
                },
            },
        )
        tid = endpoint_node_id(
            call.to_repo or endpoint.repo_id, call.to_service, call.to_method, path
        )
        if tid not in snap.nodes:
            tid = external_node_id(
                call.to_method, f"{call.to_repo or endpoint.repo_id}/{call.to_service}:{path}"
            )
            warnings.append(
                f"call target not indexed ({call.to_service} {call.to_method.upper()} {path}); "
                "it will be recorded as an ExternalApi placeholder"
            )
        effects.append(
            {"kind": "CALLS_API", "from_api": api_label, "to": tid, "confidence": "MANUAL"}
        )

    suppress_ids = {s.id for s in service.suppress}
    handlers = snap.handlers_of(endpoint_id)
    closure: set[str] = set()
    for handler in handlers:
        closure.update(snap.call_closure(handler.id))
    for item in body.suppress:
        entry_id = _unique_id(suppress_ids, f"fix-{base}-suppress")
        value: dict[str, Any] = {"id": entry_id, "from_api": api_label, "reason": item.reason}
        if item.to_service:
            value["to_service"] = item.to_service
        if item.from_client:
            value["from_client"] = item.from_client
        editor.edit("add", f"suppress:{endpoint.service_id}/{entry_id}", value)
        removed = sum(
            1
            for e in snap.edges
            if e.kind == EdgeKind.CALLS_API
            and e.from_id in closure
            and (
                not item.to_service
                or (snap.nodes.get(e.to_id) and snap.nodes[e.to_id].service_id == item.to_service)
            )
        )
        effects.append({"kind": "SUPPRESS", "from_api": api_label, "matching_edges_now": removed})

    plan = editor.plan()
    return (
        editor,
        plan,
        {
            "endpoint": endpoint_id,
            "service": endpoint.service_id,
            "diffs": plan.diffs,
            "ops": [{"action": op.action, "entity": op.entity} for op in plan.ops],
            "effects": effects,
            "warnings": warnings + plan.warnings,
        },
    )


def apply_plan(
    root: Path, snap: GraphSnapshot, endpoint_id: str, body: CorrectionRequest
) -> dict[str, Any]:
    editor, plan, preview = build_plan(root, snap, endpoint_id, body)
    editor.apply(plan)
    return {**preview, "applied": True}


async def draft_from_prompt(
    llm: Any, snap: GraphSnapshot, endpoint_id: str, prompt: str
) -> tuple[CorrectionRequest, list[str]]:
    """Draft a structured correction from a prompt; invalid parts are dropped."""
    endpoint = snap.nodes[endpoint_id]
    symbols = [
        n.props.get("qualified", "") or n.name
        for n in snap.by_kind(NodeKind.SYMBOL)
        if n.repo_id == endpoint.repo_id and n.service_id == endpoint.service_id
    ][:80]
    targets = [
        f"{n.service_id} {n.props.get('method', '')} {n.props.get('path', '')}"
        for n in snap.by_kind(NodeKind.API_ENDPOINT)
        if n.repo_id == endpoint.repo_id
    ][:80]
    system = (
        "You draft ONE structured correction for a mis-parsed API in a code graph. "
        'Reply with only JSON: {"handler": "qualified symbol or empty", '
        '"add_calls": [{"to_service": "", "to_method": "GET", "to_path": "/x", '
        '"evidence_file": "repo/relative.py", "evidence_line": 1, '
        '"evidence_text": "source text"}], '
        '"suppress": [{"to_service": "", "reason": "why"}]}. '
        "Use only handler symbols and target APIs from the lists given; never invent evidence."
    )
    user = json.dumps(
        {
            "api": f"{endpoint.props.get('method', '')} {endpoint.props.get('path', '')}",
            "service": endpoint.service_id,
            "current_handlers": [
                h.props.get("qualified", "") or h.name for h in snap.handlers_of(endpoint_id)
            ],
            "handler_symbols_in_service": symbols,
            "known_target_apis": targets,
            "user_prompt": prompt,
        }
    )
    raw = await llm.chat(system, user)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        data = json.loads(raw)
    except ValueError:
        return CorrectionRequest(), ["model draft was not valid JSON; nothing drafted"]
    if not isinstance(data, dict):
        return CorrectionRequest(), ["model draft was not a JSON object; nothing drafted"]
    warnings: list[str] = []
    try:
        draft = CorrectionRequest.model_validate(
            {k: data.get(k, d) for k, d in (("handler", ""), ("add_calls", []), ("suppress", []))}
        )
    except ValueError as exc:
        return CorrectionRequest(), [f"model draft failed validation: {exc}"]
    if draft.handler:
        try:
            resolve_handler(snap, endpoint, draft.handler)
        except ValueError:
            warnings.append(f"dropped unresolvable handler draft: {draft.handler}")
            draft.handler = ""
    known = {
        (n.service_id, n.props.get("method", ""), n.props.get("path", ""))
        for n in snap.by_kind(NodeKind.API_ENDPOINT)
    }
    kept = []
    for call in draft.add_calls:
        if (call.to_service, call.to_method.upper(), normalize_path(call.to_path)) not in known:
            warnings.append(
                f"dropped call draft to unindexed API: {call.to_service} {call.to_path}"
            )
            continue
        kept.append(call)
    draft.add_calls = kept
    draft.prompt = prompt
    return draft, warnings
