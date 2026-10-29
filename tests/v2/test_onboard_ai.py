from __future__ import annotations

import asyncio

import pytest

from axon.v2.onboard import OnboardReport
from axon.v2.onboard_ai import ai_draft

pytestmark = pytest.mark.tier1


class FakeLlm:
    model = "fake-model"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def chat(self, system: str, user: str) -> str:
        self.calls.append(user)
        return self.reply


def _report() -> OnboardReport:
    return OnboardReport(
        draft_yaml=(
            "version: 1\nrepo: r\nservices:\n"
            "- id: orders\n  roots: [services/orders]\n"
            "  clients:\n  - id: paymentscli\n    paths: [clients/paymentscli]\n"
            "- id: payments\n  roots: [services/payments]\n"
            "  specs: [specs/payments.yaml]\n"
        ),
        services=[
            {
                "id": "orders",
                "roots": ["services/orders"],
                "specs": [],
                "clients": [{"id": "paymentscli", "paths": ["clients/paymentscli"], "clients": []}],
            },
            {
                "id": "payments",
                "roots": ["services/payments"],
                "specs": ["specs/payments.yaml"],
                "clients": [],
            },
        ],
        ambiguities=["Possible generated client at clients/paymentscli"],
        notes=[],
        evidence={"orders": ["services/orders/app.py"]},
    )


def test_grounded_suggestion_is_applied_with_provenance() -> None:
    llm = FakeLlm(
        '{"suggestions": [{"client": "paymentscli", '
        '"target_service": "payments", "spec": "specs/payments.yaml", '
        '"reason": "client package matches the payments spec"}]}'
    )
    updated, applied, rejected = asyncio.run(ai_draft(_report(), llm))
    assert rejected == []
    assert len(applied) == 1 and "fake-model" in applied[0]
    client = updated.services[0]["clients"][0]
    assert client["target_service"] == "payments"
    assert client["spec"] == "specs/payments.yaml"
    assert "target_service: payments" in updated.draft_yaml
    assert updated.draft_yaml.startswith("# Fields marked by AI suggestion")


def test_ungrounded_suggestions_are_dropped() -> None:
    llm = FakeLlm(
        '{"suggestions": [{"client": "paymentscli", '
        '"target_service": "billing", "spec": "specs/billing.yaml", '
        '"reason": "sounds right"}]}'
    )
    updated, applied, rejected = asyncio.run(ai_draft(_report(), llm))
    assert applied == []
    assert len(rejected) == 2
    assert updated.services[0]["clients"][0].get("target_service") in ("", None)


def test_unusable_reply_leaves_draft_unchanged() -> None:
    report = _report()
    updated, applied, rejected = asyncio.run(ai_draft(report, FakeLlm("no json here")))
    assert applied == [] and len(rejected) == 1
    assert updated.draft_yaml == report.draft_yaml


def test_no_pending_clients_skips_the_model() -> None:
    report = _report()
    report.services[0]["clients"][0]["target_service"] = "payments"
    llm = FakeLlm("{}")
    updated, applied, rejected = asyncio.run(ai_draft(report, llm))
    assert llm.calls == [] and applied == [] and rejected == []
    assert updated is report
