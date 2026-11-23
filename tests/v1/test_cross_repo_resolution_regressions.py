import json
from pathlib import Path

import pytest

from axon.v1.diagnose import link_diagnosis
from axon.v1.endpoint_catalog import EndpointCatalog
from axon.v1.facts import HttpFact
from axon.v1.indexer import index_repo
from axon.v1.linker import resolve_http
from axon.v1.models import EdgeKind, NodeKind, endpoint_node_id
from axon.v1.servicemap import ClientDecl, ServiceDecl, ServiceMap
from axon.v1.store import MemoryStore


def _index(tmp_path: Path, repo: str, services: list[dict], source: str, *, store):
    root = tmp_path / repo
    root.mkdir()
    (root / "axon.yaml").write_text(
        json.dumps({"version": 1, "repo": repo, "services": services}), encoding="utf-8"
    )
    for service in services:
        for path in service["roots"]:
            directory = root / path
            directory.mkdir()
            (directory / "app.py").write_text(
                source if path != "admin" else "pass\n", encoding="utf-8"
            )
    snapshot, _ = index_repo(root, workers=1, use_cache=False)
    store.publish_repository(snapshot, repo_id=repo)


def _target(tmp_path, store, repo, *paths, bases=()):
    source = "from fastapi import FastAPI\napp = FastAPI()\n"
    for i, path in enumerate(paths):
        source += f"\n@app.get({path!r})\ndef get_entries_{i}():\n    return {{}}\n"
    _index(
        tmp_path,
        repo,
        [{"id": "ledger", "roots": ["ledger"], "base_urls": list(bases)}],
        source,
        store=store,
    )


def _caller(tmp_path, store, expression, *, orders_clients=(), admin_clients=()):
    _index(
        tmp_path,
        "A",
        [
            {"id": "orders", "roots": ["orders"], "clients": list(orders_clients)},
            {"id": "admin", "roots": ["admin"], "clients": list(admin_clients)},
        ],
        (
            "from fastapi import FastAPI\nimport os\nimport requests\napp = FastAPI()\n"
            '@app.get("/start")\ndef start():\n'
            f"    requests.get({expression})\n"
            "    return {}\n"
        ),
        store=store,
    )
    snap = store.load()
    (edge,) = (e for e in snap.edges if e.kind == EdgeKind.CALLS_API)
    return snap, edge


def _client(repo):
    return {
        "id": f"ledger-client-{repo}",
        "target_repo": repo,
        "target_service": "ledger",
        "packages": ["ledger_sdk"],
        "base_urls": ["env:BASE_URL"],
    }


@pytest.mark.parametrize("owns_mapping", [False, True])
def test_symbolic_client_base_stays_in_caller_service(tmp_path, owns_mapping):
    store = MemoryStore()
    _target(tmp_path, store, "B", "/entries")
    _target(tmp_path, store, "C", "/entries")
    snap, edge = _caller(
        tmp_path,
        store,
        'os.environ["BASE_URL"] + "/entries"',
        orders_clients=[_client("B")] if owns_mapping else [],
        admin_clients=[_client("C")],
    )
    if owns_mapping:
        assert edge.to_id == endpoint_node_id("B", "ledger", "GET", "/entries")
    else:
        assert snap.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API
        assert not edge.props.get("target_repo")


@pytest.mark.parametrize("reverse", [False, True])
def test_package_binding_stays_in_caller_service(tmp_path, reverse):
    store = MemoryStore()
    _target(tmp_path, store, "B", "/entries")
    _target(tmp_path, store, "C", "/entries")
    services = [
        ServiceDecl(id="admin", clients=[ClientDecl.model_validate(_client("C"))]),
        ServiceDecl(id="orders", clients=[ClientDecl.model_validate(_client("B"))]),
    ]
    sm = ServiceMap(repo="A", services=list(reversed(services)) if reverse else services)
    fact = HttpFact("run", 1, via="generated-client", call_name="ledger_sdk.get_entries_0")
    resolved = resolve_http(fact, EndpointCatalog(store.load()), sm, "A", "orders")
    assert resolved is not None
    assert resolved[0] == endpoint_node_id("B", "ledger", "GET", "/entries")


def test_imported_client_uses_calling_service_when_another_service_maps_package(tmp_path):
    store = MemoryStore()
    _target(tmp_path, store, "B", "/entries")
    _target(tmp_path, store, "C", "/entries")
    _index(
        tmp_path,
        "A",
        [
            {"id": "admin", "roots": ["admin"], "clients": [_client("C")]},
            {"id": "orders", "roots": ["orders"], "clients": [_client("B")]},
        ],
        (
            "from fastapi import FastAPI\nfrom ledger_sdk import LedgerApi\napp = FastAPI()\n"
            '@app.get("/start")\ndef start():\n'
            "    client = LedgerApi()\n"
            "    return client.get_entries_0()\n"
        ),
        store=store,
    )
    edges = [edge for edge in store.load().edges if edge.kind == EdgeKind.CALLS_API]
    assert [edge.to_id for edge in edges] == [endpoint_node_id("B", "ledger", "GET", "/entries")]


@pytest.mark.parametrize(
    ("base", "url", "path", "bound"),
    [
        ("https://gw.example/ledger", "https://gw.example/ledger/entries", "/entries", {}),
        ("https://gw.example/ledger", "https://gw.example/ledger", "/", {}),
        ("https://gw.example", "HTTPS://gw.example", "/", {}),
        ("ledger", "http://ledger/entries", "/entries", {}),
        ("ledger", "https://ledger/entries", "/entries", {}),
        ("http://ledger", "http://ledger/items/42", "/items/{id}", {"id": "42"}),
    ],
)
def test_declared_base_links_supported_routes(tmp_path, base, url, path, bound):
    store = MemoryStore()
    _target(tmp_path, store, "B", path, bases=[base])
    _, edge = _caller(tmp_path, store, repr(url))
    assert edge.to_id == endpoint_node_id("B", "ledger", "GET", path)
    assert json.loads(edge.props.get("bound", "{}")) == bound


def test_conflicting_mount_aliases_stay_external(tmp_path):
    store = MemoryStore()
    _target(
        tmp_path, store, "B", "/ledger/entries", "/entries", bases=["https://gw.example/ledger"]
    )
    snap, edge = _caller(tmp_path, store, repr("https://gw.example/ledger/entries"))
    assert snap.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API
    diagnosis = link_diagnosis(snap, edge)
    assert diagnosis["reason"] == "target-ambiguous"
    assert len(diagnosis["candidates"]) == 2


def test_scheme_neutral_call_does_not_choose_between_http_and_https_services(tmp_path):
    store = MemoryStore()
    _target(tmp_path, store, "B", "/entries", bases=["http://ledger:8888"])
    _target(tmp_path, store, "C", "/other", bases=["https://ledger:8888"])
    snap, edge = _caller(tmp_path, store, repr("//ledger:8888/entries"))
    assert snap.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API
    diagnosis = link_diagnosis(snap, edge)
    assert diagnosis["reason"] == "target-ambiguous"
    assert diagnosis["candidates"] == [endpoint_node_id("B", "ledger", "GET", "/entries")]


def test_one_malformed_url_does_not_abort_index(tmp_path):
    store = MemoryStore()
    _target(tmp_path, store, "B", "/entries", bases=["http://ledger"])
    snap, edge = _caller(tmp_path, store, repr("http://[bad/entries"))
    assert snap.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API
    assert endpoint_node_id("A", "orders", "GET", "/start") in snap.nodes
    assert snap.link_reports["A"].unresolved


def test_missing_bound_endpoint_has_no_ineligible_diagnostic_choices(tmp_path):
    store = MemoryStore()
    _target(tmp_path, store, "B", "/other")
    _target(tmp_path, store, "C", "/entries")
    _target(tmp_path, store, "D", "/entries")
    snap, edge = _caller(
        tmp_path, store, 'os.environ["BASE_URL"] + "/entries"', orders_clients=[_client("B")]
    )
    assert snap.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API
    diagnosis = link_diagnosis(snap, edge)
    assert diagnosis["reason"] == "target-endpoint-missing"
    assert diagnosis["target_repo"] == "B"
    assert diagnosis["candidates"] == []
