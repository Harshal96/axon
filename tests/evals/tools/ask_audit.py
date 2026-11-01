"""Opt-in live Ask audit against human-verified contracts; never generates goldens.

Run: uv run python -m tests.evals.tools.ask_audit --api http://localhost:8080 --golden cases.json
The golden file is a JSON list using the Ask eval case schema. Expected rows
contain full graph node IDs and must be checked against source by the author.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

from axon.v2.ask import AskResponse
from tests.evals.ask.harness import check_response


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--golden", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.golden.read_text())
    failures = 0
    with httpx.Client(base_url=args.api.rstrip("/"), timeout=60) as client:
        for case in cases:
            start = time.perf_counter()
            try:
                response = client.post(
                    "/api/v2/ask",
                    json={
                        "question": case["question"],
                        "engine": case.get("engine", "deterministic"),
                    },
                )
                response.raise_for_status()
                result = check_response(
                    case,
                    AskResponse.model_validate(response.json()),
                    (time.perf_counter() - start) * 1000,
                )
            except (httpx.HTTPError, ValueError) as exc:
                result = {"passed": False, "failures": [str(exc)]}
            print(f"{'PASS' if result['passed'] else 'FAIL'} {case['id']}")
            for failure in result["failures"]:
                print(f"  - {failure}")
            failures += not result["passed"]
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
