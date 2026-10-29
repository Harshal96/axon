"""Benchmark index_repo on a repo and report per-phase timings.

Usage: uv run python tests/perf/bench_index.py REPO [--workers N] [--rounds N] [--cold] [--json]

Runs in-process; rounds 2+ see a warm facts cache unless --cold points
each round at a fresh cache dir. Reports medians plus peak RSS of this
process. Orientation numbers, not CI gates.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import resource
import statistics
import sys
import tempfile

sys.path.insert(0, os.getcwd())

from axon.v2.indexer import IndexTimings, index_repo  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--cold", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rounds: list[dict] = []
    nodes = edges = 0
    for r in range(args.rounds):
        if args.cold:
            os.environ["AXON_FACTS_CACHE_DIR"] = tempfile.mkdtemp(prefix=f"axon-bench-{r}-")
        timings = IndexTimings()
        snap, _ = index_repo(args.repo, workers=args.workers, timings=timings)
        nodes, edges = len(snap.nodes), len(snap.edges)
        rounds.append(dataclasses.asdict(timings))

    fields = [k for k in rounds[0] if k.endswith("_s")]
    median = {f: round(statistics.median(r[f] for r in rounds), 3) for f in fields}
    result = {
        "repo": args.repo,
        "workers": args.workers,
        "rounds": args.rounds,
        "nodes": nodes,
        "edges": edges,
        "median_s": median,
        "files": {k: rounds[-1][k] for k in rounds[-1] if k.startswith("files_")},
        # ru_maxrss is bytes on macOS, KiB on Linux.
        "peak_rss_mb": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            / (1024 * 1024 if sys.platform == "darwin" else 1024),
            1,
        ),
    }
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        print(f"nodes={nodes} edges={edges} peak_rss={result['peak_rss_mb']}MB")
        for name, value in median.items():
            print(f"{name}: {value:.3f}s")
        for name, value in result["files"].items():
            print(f"{name}: {value}")


if __name__ == "__main__":
    main()
