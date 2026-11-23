"""Disposable high-outbound publication measurement; no target code execution."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import resource
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

import yaml

from axon.v1.indexer import index_repo
from axon.v1.models import EdgeKind, NodeKind
from axon.v1.store import MemoryStore, Neo4jStore


def generate(root: Path, index: int, repos: int, files: int, calls: int) -> None:
    source = root / "src"
    source.mkdir(parents=True)
    (root / "axon.yaml").write_text(
        yaml.safe_dump(
            {
                "repo": f"scale-{index}",
                "services": [
                    {
                        "id": "ledger",
                        "roots": ["src"],
                        "base_urls": [f"https://scale-{index}.example"],
                    }
                ],
            }
        )
    )
    for file in range(files):
        text = "import requests\nfrom fastapi import FastAPI\napp = FastAPI()\n"
        route = "/items" if file == 0 else f"/run/{file}"
        text += f"@app.get({route!r})\n"
        text += f"def handler_{file}():\n"
        target = (index + 1) % repos
        text += "".join(
            f"    value_{call} = requests.get('https://scale-{target}.example/items')\n"
            for call in range(calls)
        )
        text += "    return {}\n"
        (source / f"module_{file}.py").write_text(text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", choices=["memory", "neo4j"], default="memory")
    parser.add_argument("--repos", type=int, default=4)
    parser.add_argument("--files", type=int, default=25)
    parser.add_argument("--calls", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repos < 2 or args.files < 1 or args.calls < 1:
        parser.error("need at least 2 repos, 1 file per repo and 1 call per file")
    tracemalloc.start()
    driver = None
    with tempfile.TemporaryDirectory(prefix="axon-multirepo-scale-") as directory:
        root = Path(directory)
        os.environ["AXON_STATE_DIR"] = str(root / "state")
        os.environ["AXON_FACTS_CACHE_DIR"] = str(root / "facts")
        os.environ["AXON_INDEX_WORKERS"] = "1"
        if args.store == "neo4j":
            from neo4j import GraphDatabase

            uri = os.environ.get("AXON_TEST_NEO4J_URI", "")
            if uri != "bolt://localhost:17687":
                raise ValueError("use only the dedicated disposable bolt://localhost:17687")
            driver = GraphDatabase.driver(
                uri,
                auth=(os.environ["AXON_TEST_NEO4J_USER"], os.environ["AXON_TEST_NEO4J_PASSWORD"]),
            )
            with driver.session() as session:
                if session.run("MATCH (n) RETURN count(n) AS count").single()["count"] != 0:
                    driver.close()
                    raise ValueError("refusing nonempty dedicated database")
            store = Neo4jStore()
            store._session = driver.session
            store.ensure_schema()
        else:
            store = MemoryStore()
        measurements = []
        try:
            for index in range(args.repos):
                path = root / f"repo-{index}"
                generate(path, index, args.repos, args.files, args.calls)
                started = time.perf_counter()
                snap, _ = index_repo(path)
                parse_s = time.perf_counter() - started
                started = time.perf_counter()
                published = store.publish_repository(snap, repo_id=f"scale-{index}")
                measurements.append(
                    {
                        "phase": f"initial-{index}",
                        "parse_s": parse_s,
                        "publish_wall_s": time.perf_counter() - started,
                        "stats": dataclasses.asdict(published.publication_stats),
                    }
                )
            path = root / "repo-1" / "src" / "module_0.py"
            path.write_text(path.read_text().replace("@app.get('/items')", "@app.get('/items-v2')"))
            started = time.perf_counter()
            snap, _ = index_repo(root / "repo-1")
            parse_s = time.perf_counter() - started
            started = time.perf_counter()
            published = store.publish_repository(snap, repo_id="scale-1")
            measurements.append(
                {
                    "phase": "target-route-update",
                    "parse_s": parse_s,
                    "publish_wall_s": time.perf_counter() - started,
                    "stats": dataclasses.asdict(published.publication_stats),
                }
            )
            report = {
                "store": args.store,
                "repositories": args.repos,
                "files": args.repos * args.files,
                "sites": sum(
                    len(value.prepared) + len(value.pinned)
                    for value in published.link_inputs.values()
                ),
                "nodes": len(published.nodes),
                "edges": len(published.edges),
                "link_rows": sum(edge.kind == EdgeKind.CALLS_API for edge in published.edges),
                "resolved_rows": sum(
                    edge.kind == EdgeKind.CALLS_API
                    and published.nodes[edge.to_id].kind == NodeKind.API_ENDPOINT
                    for edge in published.edges
                ),
                "measurements": measurements,
                "tracemalloc_peak_bytes": tracemalloc.get_traced_memory()[1],
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1 if sys.platform == "darwin" else 1024),
                "limits": (
                    "Synthetic local single-process publication, serial extraction; "
                    "RSS excludes Neo4j container; tracemalloc adds overhead; "
                    "not a production latency or capacity qualification."
                ),
            }
            assert report["sites"] == args.repos * args.files * args.calls
            assert report["link_rows"] == report["sites"]
            assert report["resolved_rows"] == report["sites"] - args.files * args.calls
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(
                json.dumps(
                    {key: value for key, value in report.items() if key != "measurements"}, indent=2
                )
            )
        finally:
            if driver is not None:
                with driver.session() as session:
                    session.run("MATCH (n) DETACH DELETE n").consume()
                driver.close()


if __name__ == "__main__":
    main()
