"""Contracts for private bounded link metadata and public generation guards."""

from __future__ import annotations

import copy

import pytest

from axon.v1.facts import HttpFact
from axon.v1.link_inputs import PinnedCallInput, PreparedCallInput, RepoLinkInputs, RepoLinkReport
from axon.v1.models import Evidence, GraphEdge, GraphSchemaError
from axon.v1.servicemap import ServiceMap

pytestmark = pytest.mark.tier1


def _inputs() -> dict[str, RepoLinkInputs]:
    return {
        "r-雪": RepoLinkInputs(
            repo_id="r-雪",
            service_map=ServiceMap.model_validate(
                {
                    "repo": "r-雪",
                    "services": [{"id": "app", "roots": ["src"], "custom": {"x": "雪"}}],
                    "custom": ["effective", "map"],
                }
            ),
            source_generation="opaque-source",
            source_commit="abc123",
            manual_sources={"/tmp/覆盖.yaml": "hash"},
            prepared=[
                PreparedCallInput(
                    "caller",
                    "src/雪.py",
                    "service",
                    "app",
                    2,
                    "",
                    HttpFact(
                        "f",
                        12,
                        method="GET",
                        url="/雪",
                        via="httpx",
                        client_id="client",
                        client_owner="app",
                        binding_rule="factory",
                        bound={"base": "https://雪"},
                        provider="payments",
                        callee="fetch",
                        detail="evidence",
                    ),
                )
            ],
            pinned=[
                PinnedCallInput(
                    GraphEdge(
                        from_id="caller",
                        to_id="target",
                        kind="CALLS_API",
                        confidence="MANUAL",
                        evidence=Evidence(
                            file="overrides.yaml", line=9, rule="manual", detail="雪"
                        ),
                        props={"metadata": "complete"},
                    ),
                    "target",
                    "target name",
                    "other-repo",
                    "pay",
                    "GET",
                    "/雪",
                )
            ],
            static_suppression_counts={"reason": 7},
        )
    }


def test_typed_metadata_roundtrip_preserves_inputs_and_reports():
    from axon.v1.link_publication import (
        decode_link_inputs,
        decode_link_reports,
        encode_link_inputs,
        encode_link_reports,
    )

    inputs = _inputs()
    before = copy.deepcopy(inputs)
    reports = {"r-雪": RepoLinkReport(["missing-雪"], ["detail"], {"api": {"linked": 3}})}
    rows = encode_link_inputs(inputs, owner="opaque-owner", generation="opaque-generation")
    assert all(
        row["owner"] == "opaque-owner" and row["generation"] == "opaque-generation" for row in rows
    )
    assert decode_link_inputs(reversed(rows)) == inputs
    assert decode_link_reports(encode_link_reports(reports, owner="x", generation="y")) == reports
    assert inputs == before
    assert decode_link_inputs([]) == {}
    assert decode_link_reports([]) == {}


def test_metadata_payload_has_a_fixed_bound_and_detects_missing_chunks():
    from axon.v1.link_publication import (
        MAX_PAYLOAD_CHARS,
        decode_link_inputs,
        encode_link_inputs,
    )

    inputs = _inputs()
    inputs["r-雪"].manual_sources["large"] = "0123456789雪" * MAX_PAYLOAD_CHARS
    rows = encode_link_inputs(inputs, owner="o", generation="g")
    assert len(rows) > 2
    assert all(0 < len(row["payload"]) <= MAX_PAYLOAD_CHARS for row in rows)
    assert decode_link_inputs(rows) == inputs
    with pytest.raises(GraphSchemaError, match="incomplete.*re-index"):
        decode_link_inputs(rows[:-1])


@pytest.mark.parametrize(
    "change",
    [
        {"format_version": 2},
        {"format_version": True},
        {"chunk_count": 0},
        {"chunk_index": -1},
        {"payload": "%%%"},
        {"sha256": "0" * 64},
        {"kind": "future"},
        {"owner": None},
        {"extra": "unexpected"},
    ],
)
def test_metadata_rejects_corrupt_and_future_rows(change):
    from axon.v1.link_publication import decode_link_inputs, encode_link_inputs

    rows = encode_link_inputs(_inputs(), owner="o", generation="g")
    rows[0].update(change)
    with pytest.raises(GraphSchemaError, match="link metadata.*[Uu]pgrade|link metadata.*re-index"):
        decode_link_inputs(rows)


def test_metadata_rejects_duplicate_mixed_tags_wrong_keys_and_input_versions():
    from axon.v1.link_publication import decode_link_inputs, encode_link_inputs

    rows = encode_link_inputs(_inputs(), owner="o", generation="g")
    for invalid in (
        [*rows, *rows],
        [*rows, *encode_link_inputs(_inputs(), owner="o", generation="other")],
    ):
        with pytest.raises(GraphSchemaError):
            decode_link_inputs(invalid)
    changed = copy.deepcopy(rows)
    changed[0]["key"] = "wrong-repo"
    with pytest.raises(GraphSchemaError):
        decode_link_inputs(changed)
    inputs = _inputs()
    inputs["r-雪"].format_version = 2
    with pytest.raises(GraphSchemaError, match="format.*2"):
        encode_link_inputs(inputs, owner="o", generation="g")


def test_reports_are_bounded_per_repo_and_source_objects_are_independent():
    from axon.v1.link_publication import (
        MAX_PAYLOAD_CHARS,
        decode_link_reports,
        encode_link_reports,
    )

    reports = {
        "one": RepoLinkReport(["雪" * MAX_PAYLOAD_CHARS], ["detail"], {"api": {"linked": 5}}),
        "two": RepoLinkReport(["missing"], [], {}),
    }
    before = copy.deepcopy(reports)
    rows = encode_link_reports(reports, owner="o", generation="g")
    assert len(rows) > 2
    decoded = decode_link_reports(reversed(rows))
    assert decoded == reports == before
    decoded["one"].unresolved.append("changed")
    assert reports == before
    with pytest.raises(GraphSchemaError, match="incomplete"):
        decode_link_reports(rows[1:])


def test_metadata_rejects_missing_fields_oversized_payloads_and_typed_corruption():
    import base64
    import hashlib
    import json

    from axon.v1.link_publication import (
        MAX_PAYLOAD_CHARS,
        decode_link_inputs,
        decode_link_reports,
        encode_link_inputs,
    )

    rows = encode_link_inputs(_inputs(), owner="o", generation="g")
    assert len(rows) == 1
    for name in rows[0]:
        changed = copy.deepcopy(rows)
        del changed[0][name]
        with pytest.raises(GraphSchemaError):
            decode_link_inputs(changed)
    changed = copy.deepcopy(rows)
    changed[0]["payload"] = "a" * (MAX_PAYLOAD_CHARS + 1)
    with pytest.raises(GraphSchemaError):
        decode_link_inputs(changed)
    with pytest.raises(GraphSchemaError):
        decode_link_reports(rows)
    original = json.loads(base64.b64decode(rows[0]["payload"]))
    for mutate in (
        lambda data: data.update({"format_version": 2}),
        lambda data: data["prepared"][0]["fact"].update({"line": "bad-int"}),
    ):
        data = copy.deepcopy(original)
        mutate(data)
        raw = json.dumps(data).encode()
        changed = copy.deepcopy(rows)
        changed[0].update(
            payload=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest()
        )
        with pytest.raises(GraphSchemaError):
            decode_link_inputs(changed)


def test_metadata_record_size_limit_applies_on_encode_and_decode(monkeypatch):
    import axon.v1.link_publication as publication

    inputs = _inputs()
    rows = publication.encode_link_inputs(inputs, owner="o", generation="g")
    monkeypatch.setattr(publication, "MAX_RECORD_BYTES", 32)
    with pytest.raises(GraphSchemaError, match="exceeds"):
        publication.encode_link_inputs(inputs, owner="o", generation="g")
    with pytest.raises(GraphSchemaError, match="corrupt"):
        publication.decode_link_inputs(rows)


def test_metadata_encoder_reports_invalid_typed_values_without_mutation():
    from axon.v1.link_publication import encode_link_inputs

    inputs = _inputs()
    inputs["r-雪"].prepared[0].fact.line = "bad-int"
    before = copy.deepcopy(inputs)
    with pytest.raises(GraphSchemaError, match="link metadata.*re-index"):
        encode_link_inputs(inputs, owner="o", generation="g")
    assert inputs == before
