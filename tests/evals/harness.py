"""Semantic language/framework evals for Axon v2.

The cases in ``cases/*.json`` are human-authored contracts, not generated
snapshots. Each one indexes a small fixture repository and checks the
services, endpoints, handlers, outbound links, cross-service call aggregation, and
unresolved-call count that the fixture is supposed to produce.

Run directly for a compact report::

    uv run python -m tests.evals.harness
    uv run python -m tests.evals.harness python-fastapi-router
    uv run python -m tests.evals.harness --json eval-report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from axon.v2.graph import GraphSnapshot
from axon.v2.indexer import index_repo
from axon.v2.models import GraphNode, NodeKind

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = Path(__file__).resolve().parent
CASE_DIR = EVAL_DIR / "cases"
FIXTURE_ROOT = ROOT / "tests" / "fixtures"


@dataclass(frozen=True)
class EvalCase:
    id: str
    language: str
    framework: str
    repo: str
    variant: str = ""
    support: str = "supported"
    services: tuple[str, ...] = ()
    endpoints: tuple[dict[str, Any], ...] = ()
    links: tuple[dict[str, str], ...] = ()
    service_map: tuple[dict[str, Any], ...] | None = None
    unresolved_count: int | None = None
    source_files: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalCase:
        required = ("id", "language", "framework", "repo")
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise ValueError(f"eval case is missing required keys: {missing}")
        support = str(data.get("support", "supported"))
        if support not in ("supported", "known-gap"):
            raise ValueError(f"{data['id']}: unsupported support value {support!r}")
        return cls(
            id=str(data["id"]),
            language=str(data["language"]),
            framework=str(data["framework"]),
            repo=str(data["repo"]),
            variant=str(data.get("variant", "")),
            support=support,
            services=tuple(str(v) for v in data.get("services", [])),
            endpoints=tuple(dict(v) for v in data.get("endpoints", [])),
            links=tuple(
                {"from": str(v["from"]), "to": str(v["to"])} for v in data.get("links", [])
            ),
            service_map=(
                tuple(dict(v) for v in data["service_map"]) if "service_map" in data else None
            ),
            unresolved_count=(
                int(data["unresolved_count"]) if "unresolved_count" in data else None
            ),
            source_files=int(data["source_files"]) if "source_files" in data else None,
        )


@dataclass
class CaseResult:
    case: EvalCase
    failures: list[str] = field(default_factory=list)
    endpoint_hits: int = 0
    link_hits: int = 0

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case.id,
            "language": self.case.language,
            "framework": self.case.framework,
            "variant": self.case.variant,
            "support": self.case.support,
            "passed": self.passed,
            "endpoints": {
                "matched": self.endpoint_hits,
                "expected": len(self.case.endpoints),
            },
            "links": {
                "matched": self.link_hits,
                "expected": len(self.case.links),
            },
            "failures": list(self.failures),
        }


def load_matrix() -> dict[str, Any]:
    data = json.loads((EVAL_DIR / "matrix.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("tests/evals/matrix.json must contain a JSON object")
    return data


def load_cases() -> list[EvalCase]:
    cases = [
        EvalCase.from_dict(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(CASE_DIR.glob("*.json"))
    ]
    ids = [case.id for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise ValueError(f"duplicate eval case ids: {duplicates}")
    return cases


@cache
def _snapshot(repo: str) -> GraphSnapshot:
    repo_path = FIXTURE_ROOT / repo
    snapshot, _service_map = index_repo(str(repo_path))
    return snapshot


def _endpoint_ref(node: GraphNode) -> str:
    return f"{node.service_id}:{node.props.get('method', '')} {node.props.get('path', '')}"


def _target_ref(node: GraphNode) -> str:
    if node.kind == NodeKind.EXTERNAL_API:
        return f"external:{node.name}"
    return _endpoint_ref(node)


def _expected_endpoint_ref(endpoint: dict[str, Any]) -> str:
    return f"{endpoint['service']}:{endpoint['method']} {endpoint['path']}"


def _set_failure(result: CaseResult, label: str, expected: set[str], observed: set[str]) -> None:
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    if missing:
        result.failures.append(f"{label} missing: {missing}")
    if unexpected:
        result.failures.append(f"{label} unexpected: {unexpected}")


def _observed_service_map(snapshot: GraphSnapshot) -> list[dict[str, Any]]:
    """Cross-service call aggregation, derived here from outbound_calls.

    The product has no whole-graph service map anymore; eval cases
    still pin this aggregation as a parser/linker contract.
    """
    nodes = snapshot.nodes

    def label(node_id: str) -> str:
        node = nodes.get(node_id)
        if node is None:
            return node_id
        if node.kind == NodeKind.EXTERNAL_API:
            return f"external:{node.name or node_id}"
        return node.service_id or node_id

    pairs: dict[tuple[str, str], int] = {}
    seen: set[tuple[str, str]] = set()
    for ep in snapshot.by_kind(NodeKind.API_ENDPOINT):
        for _edge, target in snapshot.outbound_calls(ep.id):
            if (ep.id, target.id) in seen:
                continue
            seen.add((ep.id, target.id))
            to_key = label(target.id)
            if target.kind != NodeKind.EXTERNAL_API and (
                target.repo_id,
                target.service_id,
            ) == (ep.repo_id, ep.service_id):
                continue  # same-service pair: not a cross-service edge
            key = (label(f"service::{ep.repo_id}::{ep.service_id}"), to_key)
            pairs[key] = pairs.get(key, 0) + 1

    return sorted(
        ({"from": f, "to": t, "count": c} for (f, t), c in pairs.items()),
        key=lambda edge: (edge["from"], edge["to"]),
    )


def evaluate_case(case: EvalCase) -> CaseResult:
    result = CaseResult(case=case)
    repo_path = FIXTURE_ROOT / case.repo
    if not repo_path.is_dir():
        result.failures.append(f"fixture repo does not exist: {case.repo}")
        return result

    try:
        snapshot = _snapshot(case.repo)
    except Exception as exc:  # report one broken case without hiding the rest
        result.failures.append(f"indexing failed: {type(exc).__name__}: {exc}")
        return result

    observed_services = {node.service_id for node in snapshot.by_kind(NodeKind.SERVICE)}
    _set_failure(result, "services", set(case.services), observed_services)

    if case.source_files is not None:
        observed_source_files = len(snapshot.by_kind(NodeKind.SOURCE_FILE))
        if observed_source_files != case.source_files:
            result.failures.append(
                f"source_files: expected {case.source_files}, observed {observed_source_files}"
            )

    observed_endpoints = {
        _endpoint_ref(node): node for node in snapshot.by_kind(NodeKind.API_ENDPOINT)
    }
    expected_endpoints = {_expected_endpoint_ref(endpoint): endpoint for endpoint in case.endpoints}
    result.endpoint_hits = len(set(expected_endpoints) & set(observed_endpoints))
    _set_failure(result, "endpoints", set(expected_endpoints), set(observed_endpoints))

    for ref in sorted(set(expected_endpoints) & set(observed_endpoints)):
        expected = expected_endpoints[ref]
        observed = observed_endpoints[ref]
        if "handler" in expected:
            handler = snapshot.handler_of(observed.id)
            observed_handler = handler.name if handler else None
            if observed_handler != expected["handler"]:
                result.failures.append(
                    f"endpoint {ref} handler: expected {expected['handler']!r}, "
                    f"observed {observed_handler!r}"
                )
        for key in ("framework", "source", "operation_id"):
            if key in expected and observed.props.get(key) != expected[key]:
                result.failures.append(
                    f"endpoint {ref} {key}: expected {expected[key]!r}, "
                    f"observed {observed.props.get(key)!r}"
                )

    observed_links: set[tuple[str, str]] = set()
    for endpoint in snapshot.by_kind(NodeKind.API_ENDPOINT):
        source_ref = _endpoint_ref(endpoint)
        for _edge, target in snapshot.outbound_calls(endpoint.id):
            observed_links.add((source_ref, _target_ref(target)))
    expected_links = {(link["from"], link["to"]) for link in case.links}
    result.link_hits = len(expected_links & observed_links)
    _set_failure(
        result,
        "links",
        {f"{source} -> {target}" for source, target in expected_links},
        {f"{source} -> {target}" for source, target in observed_links},
    )

    if case.service_map is not None:
        observed_map = _observed_service_map(snapshot)
        expected_map = sorted(
            (
                {"from": str(edge["from"]), "to": str(edge["to"]), "count": int(edge["count"])}
                for edge in case.service_map
            ),
            key=lambda edge: (edge["from"], edge["to"]),
        )
        if observed_map != expected_map:
            result.failures.append(f"service_map: expected {expected_map}, observed {observed_map}")

    if case.unresolved_count is not None:
        observed_unresolved = len(snapshot.unresolved)
        if observed_unresolved != case.unresolved_count:
            result.failures.append(
                "unresolved_count: "
                f"expected {case.unresolved_count}, observed {observed_unresolved}: "
                f"{snapshot.unresolved}"
            )

    return result


def format_report(results: list[CaseResult]) -> str:
    lines: list[str] = []
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        lines.append(
            f"{status} {result.case.id} "
            f"({result.case.language}/{result.case.framework}) "
            f"endpoints {result.endpoint_hits}/{len(result.case.endpoints)} "
            f"links {result.link_hits}/{len(result.case.links)}"
        )
        for failure in result.failures:
            lines.append(f"  - {failure}")
    passed = sum(result.passed for result in results)
    endpoint_hits = sum(result.endpoint_hits for result in results)
    endpoint_total = sum(len(result.case.endpoints) for result in results)
    link_hits = sum(result.link_hits for result in results)
    link_total = sum(len(result.case.links) for result in results)
    lines.append(
        f"Summary: {passed}/{len(results)} cases passed; "
        f"endpoints {endpoint_hits}/{endpoint_total}; links {link_hits}/{link_total}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Axon language/framework evals")
    parser.add_argument("cases", nargs="*", help="optional case IDs to run")
    parser.add_argument("--json", type=Path, help="write a JSON report to this path")
    args = parser.parse_args(argv)

    cases = load_cases()
    if args.cases:
        wanted = set(args.cases)
        cases = [case for case in cases if case.id in wanted]
        missing = sorted(wanted - {case.id for case in cases})
        if missing:
            parser.error(f"unknown eval case ids: {missing}")

    results = [evaluate_case(case) for case in cases]
    print(format_report(results))
    if args.json:
        payload = {
            "cases": [result.as_dict() for result in results],
            "summary": {
                "passed": sum(result.passed for result in results),
                "total": len(results),
            },
        }
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
