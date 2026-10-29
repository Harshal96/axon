"""Optional AI drafting pass for onboarding (opt-in, init-time only).

The deterministic scan in onboard.py drafts services and generated
clients but leaves client target_service/spec empty. Given a configured
language model, this module asks it to propose those two fields from
the scan's own evidence. Suggestions are validated against what the
scan actually found: a target must be a drafted service id and a spec
must be a spec the scan assigned to some service. Anything else is
dropped and reported, never applied. The indexer never sees this
module; once a human reviews and commits the draft axon.yaml, the
graph is built deterministically from it.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml  # type: ignore[import-untyped]

from axon.v2.onboard import OnboardReport
from axon.v2.servicemap import ServiceMap, dump_service_map

_SYSTEM = (
    "You complete an axon.yaml draft for a code repository. You are given "
    "the services a deterministic scan drafted and the generated clients "
    "whose target is unknown. For each client, propose target_service "
    "(the service the client was generated for) and spec (the OpenAPI "
    "file it was generated from), using only the evidence provided. "
    "If the evidence does not show it, leave the field empty. "
    'Reply with JSON only: {"suggestions": [{"client": "...", '
    '"target_service": "...", "spec": "...", "reason": "..."}]}'
)


def _parse_suggestions(raw: str) -> list[dict[str, Any]]:
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for item in data.get("suggestions", []):
        if isinstance(item, dict) and item.get("client"):
            out.append(item)
    return out


async def ai_draft(report: OnboardReport, llm: Any) -> tuple[OnboardReport, list[str], list[str]]:
    """Apply grounded model suggestions to a scan report.

    Returns (report, applied_notes, rejected_notes). The report's
    draft_yaml and services are updated in place on a copy; the input
    report is not mutated.
    """
    services = [dict(s) for s in report.services]
    pending: dict[str, dict[str, Any]] = {}
    for svc in services:
        for client in svc.get("clients", []) or []:
            if isinstance(client, dict) and not client.get("target_service"):
                pending[str(client.get("id", ""))] = client
    if not pending:
        return report, [], []
    service_ids = {str(s.get("id", "")) for s in services}
    spec_paths = {spec for s in services for spec in (s.get("specs") or [])}

    payload = {
        "services": [
            {"id": s.get("id"), "roots": s.get("roots"), "specs": s.get("specs")} for s in services
        ],
        "clients_needing_target": [
            {
                "client": cid,
                "paths": c.get("paths"),
                "packages": c.get("packages"),
                "factories": c.get("factories"),
            }
            for cid, c in pending.items()
        ],
        "evidence": report.evidence,
        "ambiguities": report.ambiguities,
    }
    raw = await llm.chat(_SYSTEM, json.dumps(payload, indent=1))
    suggestions = _parse_suggestions(raw)
    if not suggestions:
        return report, [], ["AI drafting returned no usable suggestions; draft left unchanged."]

    applied: list[str] = []
    rejected: list[str] = []
    model = str(getattr(llm, "model", "model"))
    for item in suggestions:
        client = pending.get(str(item.get("client", "")))
        if client is None:
            rejected.append(f"AI suggested a target for unknown client {item.get('client')!r}.")
            continue
        reason = str(item.get("reason", "")).strip()
        target = str(item.get("target_service", "")).strip()
        spec = str(item.get("spec", "")).strip()
        changed = []
        if target:
            if target in service_ids:
                client["target_service"] = target
                changed.append(f"target_service={target}")
            else:
                rejected.append(
                    f"AI suggestion for client {client.get('id')!r} rejected: "
                    f"target_service {target!r} is not a drafted service."
                )
        if spec:
            if spec in spec_paths:
                client["spec"] = spec
                changed.append(f"spec={spec}")
            else:
                rejected.append(
                    f"AI suggestion for client {client.get('id')!r} rejected: "
                    f"spec {spec!r} was not found by the scan."
                )
        if changed:
            applied.append(
                f"AI suggestion ({model}) for client {client.get('id')!r}: "
                f"{', '.join(changed)}"
                + (f" — {reason}" if reason else "")
                + ". Review before committing."
            )

    if not applied:
        return report, [], rejected
    data = yaml.safe_load(report.draft_yaml) or {}
    sm = ServiceMap.model_validate(data)
    by_id = {c.id: c for s in sm.services for c in s.clients}
    for svc in services:
        for client in svc.get("clients", []) or []:
            target_client = by_id.get(str(client.get("id", "")))
            if target_client is None:
                continue
            if client.get("target_service"):
                target_client.target_service = str(client["target_service"])
            if client.get("spec"):
                target_client.spec = str(client["spec"])
    header = (
        f"# Fields marked by AI suggestion ({model}) in the init report "
        "were drafted by a model and require review.\n"
    )
    updated = report.model_copy(
        update={
            "draft_yaml": header + dump_service_map(sm),
            "services": [s.model_dump() for s in sm.services],
            "notes": [*report.notes, *applied],
            "ambiguities": [*report.ambiguities, *rejected],
        }
    )
    return updated, applied, rejected
