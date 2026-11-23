from pathlib import Path
from textwrap import indent

import pytest

from axon.v1.graph import GraphSnapshot
from axon.v1.indexer import index_repo
from axon.v1.models import NodeKind
from axon.v1.store import MemoryStore


def _publish_caller(tmp_path: Path, filename: str, source: str) -> GraphSnapshot:
    checkout = tmp_path / "caller"
    checkout.mkdir()
    (checkout / "axon.yaml").write_text(
        """repo: A
services:
  - id: caller
    roots: [.]
    endpoints:
      - id: start
        method: GET
        path: /start
        handler: start
"""
    )
    (checkout / filename).write_text(source)
    target = tmp_path / "ledger"
    target.mkdir()
    (target / "axon.yaml").write_text(
        "repo: B\nservices:\n  - id: ledger\n    roots: [.]\n"
        "    base_urls: [https://ledger.example]\n"
    )
    (target / "entries.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n"
        "@app.get('/entries')\ndef entries():\n    return {}\n"
    )
    store = MemoryStore()
    for root, repo in ((checkout, "A"), (target, "B")):
        snap, _ = index_repo(str(root), workers=1, use_cache=False)
        store.publish_repository(snap, repo_id=repo)
    return store.load()


@pytest.mark.parametrize(
    ("filename", "imports", "expression"),
    [
        ("start.ts", "", 'fetch("https://ledger.example/entries")'),
        (
            "start.mjs",
            'import invoker from "axios";',
            'invoker.get("https://ledger.example/entries")',
        ),
        (
            "start.cjs",
            'const invoker = require("axios");',
            'invoker.get("https://ledger.example/entries")',
        ),
    ],
)
def test_javascript_http_calls_gain_foreign_target_on_publication(
    tmp_path, filename, imports, expression
):
    snap = _publish_caller(
        tmp_path, filename, f"{imports}\nasync function start() {{\n  return {expression};\n}}\n"
    )
    assert [node.id for node in snap.outbound_endpoints("endpoint::A::caller::GET:/start")] == [
        "endpoint::B::ledger::GET:/entries"
    ]


@pytest.mark.parametrize(
    "expression",
    [
        'fetch("https://ledger.example/entries", requestOptions)',
        'fetch("https://ledger.example/entries", { method: getMethod() })',
        'fetch("https://ledger.example/entries", { ...requestOptions })',
        "fetch(url)",
        'fetch("https://ledger.example/entries" + suffix)',
    ],
)
def test_javascript_dynamic_call_stays_visible_as_external(tmp_path, expression):
    snap = _publish_caller(tmp_path, "start.ts", f"function start() {{ return {expression}; }}")
    assert [node.kind for node in snap.outbound_endpoints("endpoint::A::caller::GET:/start")] == [
        NodeKind.EXTERNAL_API
    ]


@pytest.mark.parametrize(
    ("parameters", "body"),
    [
        ("fetch: CacheLookup", 'fetch("https://ledger.example/entries");'),
        ("", 'cache.get("https://ledger.example/entries");'),
        ("", '// fetch("https://ledger.example/entries");'),
        ("", "const s = 'fetch(\"https://ledger.example/entries\")';"),
        ("", r'const pattern = /fetch("https:\/\/ledger.example\/entries")/;'),
        ("", 'const fetch = cache.get; fetch("https://ledger.example/entries");'),
    ],
)
def test_non_transport_javascript_calls_do_not_gain_foreign_edge(tmp_path, parameters, body):
    snap = _publish_caller(
        tmp_path,
        "start.ts",
        f"function start({parameters}) {{\n{indent(body, '  ')}\n}}",
    )
    assert snap.handler_of("endpoint::A::caller::GET:/start") is not None
    assert snap.outbound_endpoints("endpoint::A::caller::GET:/start") == []
