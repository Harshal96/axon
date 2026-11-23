from pathlib import Path
from textwrap import indent

import pytest

from axon.v1.graph import GraphSnapshot
from axon.v1.indexer import index_repo
from axon.v1.store import MemoryStore


def _publish_python_call(tmp_path: Path, imports: str, handler: str) -> GraphSnapshot:
    store = MemoryStore()
    for repo, service, host, source in (
        (
            "A",
            "orders",
            "orders.example",
            f"{imports}\n@app.get('/start')\ndef start():\n{indent(handler, '    ')}\n",
        ),
        ("B", "ledger", "ledger.example", ""),
    ):
        root = tmp_path / repo
        (root / "src").mkdir(parents=True)
        (root / "axon.yaml").write_text(
            f"repo: {repo}\nservices:\n  - id: {service}\n    roots: [src]\n"
            f"    base_urls: ['https://{host}']\n"
        )
        (root / "src/app.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n"
            "@app.get('/entries')\ndef entries():\n    return {}\n" + source
        )
        snapshot, _ = index_repo(str(root), workers=1, use_cache=False)
        store.publish_repository(snapshot, repo_id=repo)
    return store.load()


@pytest.mark.parametrize(
    ("imports", "handler"),
    [
        (
            "from httpx import Client",
            "client = Client(base_url='https://ledger.example')\nclient.get('/entries')",
        ),
        (
            "from httpx import Client as LedgerClient",
            "with LedgerClient(base_url='https://ledger.example') as client:\n"
            "    client.get('/entries')",
        ),
        (
            "from httpx import Client as ledger_client",
            "ledger_client(base_url='https://ledger.example').get('/entries')",
        ),
    ],
)
def test_python_client_constructor_alias_preserves_destination(tmp_path, imports, handler):
    snap = _publish_python_call(tmp_path, imports, handler)
    assert [node.id for node in snap.outbound_endpoints("endpoint::A::orders::GET:/start")] == [
        "endpoint::B::ledger::GET:/entries"
    ]


def test_imported_python_http_function_links_remote_api(tmp_path):
    snap = _publish_python_call(
        tmp_path, "from requests import get as fetch", "fetch('https://ledger.example/entries')"
    )
    assert [node.id for node in snap.outbound_endpoints("endpoint::A::orders::GET:/start")] == [
        "endpoint::B::ledger::GET:/entries"
    ]


@pytest.mark.parametrize("binding", ["cache = {}", "cache = dict()"])
def test_dictionary_url_key_is_not_an_outbound_call(tmp_path, binding):
    snap = _publish_python_call(
        tmp_path, "", f"{binding}\ncache.get('https://ledger.example/entries')"
    )
    assert snap.outbound_endpoints("endpoint::A::orders::GET:/start") == []
