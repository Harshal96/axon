"""Generate generic multi-language repositories and measure real index phases.

Run with ``uv run python -m tools.benchmark_scale --files 30000 --symbols 10``.
The default store is memory; --store neo4j uses configured development Neo4j.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any

import yaml

from axon.v1.incremental import index_and_publish
from axon.v1.indexer import IndexTimings
from axon.v1.store import MemoryStore, Neo4jStore


def generate_repo(root: Path, *, services: int, files: int, symbols_per_file: int) -> None:
    if services < 1 or files < services or symbols_per_file < 1:
        raise ValueError("positive services/symbols and at least one file per service required")
    declarations: list[dict[str, Any]] = []
    for i in range(services):
        service = f"service-{i}"
        folder = root / service
        folder.mkdir(parents=True, exist_ok=True)
        spec = {
            "openapi": "3.0.0",
            "info": {"title": service, "version": "1"},
            "paths": {
                f"/items/{i}": {
                    "get": {
                        "operationId": f"fetchItems{i}",
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
        (folder / "openapi.yaml").write_text(yaml.safe_dump(spec))
        declarations.append(
            {"id": service, "roots": [service], "specs": [f"{service}/openapi.yaml"]}
        )
    for i in range(files):
        owner = i % services
        target = (owner + 1) % services
        folder = root / f"service-{owner}"
        language = owner % 3
        if language == 0:
            source = "import os\nimport requests\nfrom fastapi import FastAPI\napp = FastAPI()\n"
            source += f"@app.get('/local/{i}')\n"
            source += (
                f"def handler_{i}():\n"
                f"    return requests.get(f\"{{os.environ['TARGET_{target}_URL']}}"
                f'/items/{target}")\n'
            )
            source += "".join(
                f"def helper_{j}():\n    return {j}\n" for j in range(symbols_per_file - 1)
            )
            suffix = ".py"
        elif language == 1:
            source = f"class Module{i} {{\n"
            source += "".join(
                f'  public String helper{j}() {{ return "ok"; }}\n' for j in range(symbols_per_file)
            )
            source += "}\n"
            suffix = ".java"
        else:
            source = "package app\n" + "".join(
                f"func helper{i}_{j}() int {{ return {j} }}\n" for j in range(symbols_per_file)
            )
            suffix = ".go"
        (folder / f"module_{i}{suffix}").write_text(source)
    (root / "axon.yaml").write_text(
        yaml.safe_dump({"repo": "scale-fixture", "services": declarations})
    )


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


def run_benchmark(
    root: Path, *, services: int, files: int, symbols: int, store_kind: str
) -> dict[str, Any]:
    generate_repo(root, services=services, files=files, symbols_per_file=symbols)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Benchmark",
        "-c",
        "user.email=benchmark@example.invalid",
        "commit",
        "-qm",
        "Synthetic fixture",
    )
    commit = _git(root, "rev-parse", "HEAD")
    # A requested Neo4j benchmark must fail if unavailable, never silently
    # measure the memory fallback under the wrong backend label.
    store = Neo4jStore() if store_kind == "neo4j" else MemoryStore()
    ensure_schema = getattr(store, "ensure_schema", None)
    if ensure_schema:
        ensure_schema()
    state_dir = root.parent / (root.name + "-state")
    metrics: dict[str, Any] = {
        "files": files,
        "services": services,
        "symbols_per_file": symbols,
        "store": store_kind,
    }
    tracemalloc.start()
    for label in ("full", "unchanged", "edited_one_percent"):
        if label == "edited_one_percent":
            sources = sorted(root.glob("service-*/*.py"))
            sources += sorted(root.glob("service-*/*.java"))
            sources += sorted(root.glob("service-*/*.go"))
            for source in sources[: max(1, files // 100)]:
                comment = (
                    "# benchmark body edit\n"
                    if source.suffix == ".py"
                    else "// benchmark body edit\n"
                )
                with source.open("a") as handle:
                    handle.write(comment)
            _git(root, "add", ".")
            _git(
                root,
                "-c",
                "user.name=Benchmark",
                "-c",
                "user.email=benchmark@example.invalid",
                "commit",
                "-qm",
                "Edit one percent of sources",
            )
            commit = _git(root, "rev-parse", "HEAD")
        timings = IndexTimings()
        started = time.perf_counter()
        phases: dict[str, float] = {}
        last_phase = "prepare"
        phase_start = started

        def progress(phase: str, done: int, total: int) -> None:
            nonlocal last_phase, phase_start
            if phase != last_phase:
                now = time.perf_counter()
                phases[last_phase] = phases.get(last_phase, 0.0) + now - phase_start
                last_phase, phase_start = phase, now

        snap, _, result = index_and_publish(
            root, store, commit=commit, state_dir=state_dir, timings=timings, progress=progress
        )
        metrics[label] = {
            "elapsed_s": time.perf_counter() - started,
            "timings": dataclasses.asdict(timings),
            "indexing": result,
            "nodes": len(snap.nodes),
            "edges": len(snap.edges),
            "phase_s": phases,
        }
    metrics["tracemalloc_peak_bytes"] = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    metrics["peak_rss_bytes"] = rss if sys.platform == "darwin" else rss * 1024
    metrics["state_bytes"] = sum(path.stat().st_size for path in state_dir.rglob("*.db"))
    metrics["max_facts_blob_bytes"] = 0
    for path in state_dir.rglob("state.db"):
        with sqlite3.connect(path) as database:
            size = (
                database.execute(
                    "SELECT MAX(MAX(LENGTH(facts), COALESCE(LENGTH(resolved_facts), 0))) FROM files"
                ).fetchone()[0]
                or 0
            )
            metrics["max_facts_blob_bytes"] = max(metrics["max_facts_blob_bytes"], size)
    close = getattr(store, "close", None)
    if close:
        close()
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--services", type=int, default=9)
    parser.add_argument("--files", type=int, default=300)
    parser.add_argument("--symbols", type=int, default=10)
    parser.add_argument("--store", choices=("memory", "neo4j"), default="memory")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--max-rss-mib", type=int, default=6144)
    parser.add_argument("--max-facts-blob-mib", type=int, default=64)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="axon-scale-") as folder:
        report = run_benchmark(
            Path(folder) / "repo",
            services=args.services,
            files=args.files,
            symbols=args.symbols,
            store_kind=args.store,
        )
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    if report["peak_rss_bytes"] > args.max_rss_mib * 1024 * 1024:
        raise SystemExit("peak RSS exceeded configured benchmark budget")
    if report["max_facts_blob_bytes"] > args.max_facts_blob_mib * 1024 * 1024:
        raise SystemExit("per-file facts blob exceeded configured benchmark budget")


if __name__ == "__main__":
    main()
