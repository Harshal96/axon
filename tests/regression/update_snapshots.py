"""Regenerate golden output snapshots for graph operations."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

SNAPSHOTS = Path(__file__).parent / "snapshots"
SNAPSHOTS.mkdir(exist_ok=True)


async def main() -> None:
    from axon.db.init import initialize_schema
    from axon.ops.blast_radius import blast_radius
    from axon.ops.service_map import service_map
    from axon.ops.subgraph import subgraph
    from tests.fixtures.seed_graph import seed_neo4j

    initialize_schema()
    seed_neo4j()

    def save(name: str, data: dict) -> None:
        path = SNAPSHOTS / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, default=str))
        print(f"Saved {path}")

    save(
        "blast_radius_payment_lib",
        (await blast_radius("//services/payment:payment_lib", depth=3)).model_dump(),
    )
    save("subgraph_payment_lib", (await subgraph(["//services/payment:payment_lib"])).model_dump())
    save("service_map", (await service_map()).model_dump())


if __name__ == "__main__":
    asyncio.run(main())
