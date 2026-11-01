"""Hand-authored Ask contracts; run with --json [report.json] for CI/audits."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from axon.v2.ask import AskResponse, ask
from axon.v2.graph import GraphSnapshot
from axon.v2.llm import LlmClient
from tests.evals.ask.fixture import snapshot

CASE_DIR = Path(__file__).parent / "cases"


def load_cases() -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(CASE_DIR.glob("*.json"))]


async def evaluate(case: dict[str, Any], snap: GraphSnapshot | None = None) -> dict[str, Any]:
    if snap is None:
        snap = snapshot()
    start = time.perf_counter()
    scenario = case.get("model_scenario", "")

    class ContractModel(LlmClient):
        async def draft_cypher(self, question: str) -> str:
            if scenario == "unavailable":
                raise RuntimeError("contract model unavailable")
            return (
                "MATCH (n) DELETE n RETURN n"
                if scenario == "write"
                else "MATCH (n) RETURN n.id AS id"
            )

    class ContractStore:
        def run_cypher(self, cypher: str) -> list[dict[str, str]]:
            if scenario == "execution-error":
                raise RuntimeError("contract store unavailable")
            return [{"id": "entry"}] if scenario == "rows" else []

    response = await ask(
        snap,
        case["question"],
        engine=case.get("engine", "deterministic"),
        llm=ContractModel("", "") if scenario else None,
        store=ContractStore() if scenario in {"rows", "empty", "execution-error"} else None,
    )
    elapsed = (time.perf_counter() - start) * 1000
    return check_response(case, response, elapsed)


def check_response(case: dict[str, Any], response: AskResponse, elapsed: float) -> dict[str, Any]:
    expected = case["expect"]
    failures = []
    for key in ("intent", "model_used", "grounded"):
        if key in expected and getattr(response, key) != expected[key]:
            failures.append(f"{key}: expected {expected[key]!r}, got {getattr(response, key)!r}")
    if "row_count" in expected and len(response.rows) != expected["row_count"]:
        failures.append(f"row count: expected {expected['row_count']}, got {len(response.rows)}")
    pairs = sorted(
        [row.source.node_id, row.target.node_id if row.target else ""] for row in response.rows
    )
    if "rows" in expected and pairs != sorted(expected["rows"]):
        failures.append(f"rows: expected {expected['rows']!r}, got {pairs!r}")
    for text in expected.get("blind_spots_contain", []):
        if not any(text in spot for spot in response.blind_spots):
            failures.append(f"missing blind spot: {text}")
    for text in expected.get("answer_must_not_contain", []):
        if text in response.answer:
            failures.append(f"forbidden answer text: {text}")
    if "bindings" in expected:
        bound = [b["id"] for b in response.bindings if b["kind"] == "service"]
        if bound != expected["bindings"]:
            failures.append(f"bindings: expected {expected['bindings']!r}, got {bound!r}")
    if elapsed > case.get("max_ms", 500):
        failures.append(f"duration: {elapsed:.1f} ms exceeds {case['max_ms']} ms")
    return {"id": case["id"], "passed": not failures, "ms": round(elapsed, 2), "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--json", nargs="?", const="-")
    args = parser.parse_args()
    cases = [c for c in load_cases() if not args.ids or c["id"] in args.ids]
    if not cases:
        parser.error("no matching Ask cases")
    results = [asyncio.run(evaluate(case)) for case in cases]
    if args.json:
        payload = json.dumps(results, indent=2) + "\n"
        if args.json == "-":
            print(payload, end="")
        else:
            Path(args.json).write_text(payload)
    else:
        for result in results:
            print(f"{'PASS' if result['passed'] else 'FAIL'} {result['id']} ({result['ms']} ms)")
            for failure in result["failures"]:
                print(f"  - {failure}")
        print(f"Summary: {sum(r['passed'] for r in results)}/{len(results)} Ask cases passed")
    return int(any(not r["passed"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
