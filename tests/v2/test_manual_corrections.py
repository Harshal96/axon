import pytest

from axon.v2.indexer import index_repo
from axon.v2.models import Confidence, EdgeKind, NodeKind


def test_manual_endpoint_handler_call_and_provenance(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text(
        "def entry():\n    fraud.score(req)\ndef replacement():\n    pass\n"
    )
    (tmp_path / "axon.yaml").write_text("""repo: demo
services:
- id: app
  roots: [app]
  endpoints:
  - {id: entry, method: POST, path: /entry, handler: entry}
  handlers:
  - {id: override, api: 'POST /entry', handler: replacement}
  calls:
  - id: fraud
    from_api: POST /entry
    to: {service: fraud, method: POST, path: /score}
    evidence: {file: app/main.py, line: 2, text: 'fraud.score(req)'}
""")
    snap, _ = index_repo(tmp_path, workers=1, use_cache=False)
    endpoint = next(n for n in snap.by_kind(NodeKind.API_ENDPOINT) if n.props["path"] == "/entry")
    assert snap.handler_of(endpoint.id).name == "replacement"
    edge = next(e for e in snap.edges if e.kind == EdgeKind.CALLS_API)
    assert edge.confidence == Confidence.MANUAL
    assert edge.evidence.rule == "manual:axon.yaml#fraud"
    assert edge.props["source_file"] == "axon.yaml"
    assert edge.props["entry_id"] == "fraud"
    assert snap.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API


def test_scoped_links_precedence():
    from axon.v2.facts import HttpFact
    from axon.v2.graph import GraphSnapshot
    from axon.v2.linker import EndpointIndex, resolve_http
    from axon.v2.servicemap import ServiceMap

    sm = ServiceMap.model_validate(
        {
            "repo": "demo",
            "services": [
                {
                    "id": "a",
                    "roots": ["a"],
                    "links": [
                        {
                            "id": "nested",
                            "call_name": "charge",
                            "to_service": "nested",
                            "to_path": "/n",
                        }
                    ],
                }
            ],
            "links": [
                {"id": "global", "call_name": "charge", "to_service": "global", "to_path": "/g"}
            ],
        }
    )
    fact = HttpFact(caller="run", line=1, call_name="charge")
    result = resolve_http(fact, EndpointIndex(GraphSnapshot()), sm, "demo", from_service="a")
    assert "::nested::" in result[0]
    assert result[2] == "manual:axon.yaml#nested"
    result = resolve_http(fact, EndpointIndex(GraphSnapshot()), sm, "demo", from_service="b")
    assert "::global::" in result[0]


def test_overlay_provenance_suppression_and_stale_evidence(tmp_path):
    from axon.v2.doctor import doctor

    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("def run():\n    return 1\n")
    (tmp_path / "axon.yaml").write_text("""repo: demo
services:
- {id: app, roots: [app]}
overrides: [axon.overrides.yaml]
""")
    (tmp_path / "axon.overrides.yaml").write_text("""services:
- id: app
  endpoints:
  - {id: run, method: GET, path: /run, handler: run}
  calls:
  - id: declared
    from_api: GET /run
    to: {service: target, method: POST, path: /target}
    evidence: {file: app/main.py, line: 2, text: old_call()}
  suppress:
  - {id: dead, from_api: 'GET /run', to_service: target, reason: dead code}
  - {id: unused, to_service: absent, reason: stale}
""")
    snap, _ = index_repo(tmp_path, workers=1, use_cache=False)
    assert not any(e.kind == EdgeKind.CALLS_API for e in snap.edges)
    assert any("suppressed:manual#dead: 1" in d for d in snap.diagnostics)
    assert any(e.props.get("source_file") == "axon.overrides.yaml" for e in snap.edges)
    codes = {i["code"] for i in doctor(tmp_path)["issues"]}
    assert {"stale-call-evidence", "unmatched-suppress"} <= codes


def test_root_scoped_link_and_client_scope():
    from axon.v2.facts import HttpFact
    from axon.v2.graph import GraphSnapshot
    from axon.v2.linker import EndpointIndex, resolve_http
    from axon.v2.servicemap import ServiceMap

    sm = ServiceMap.model_validate(
        {
            "repo": "r",
            "links": [
                {
                    "id": "client",
                    "from_client": "sdk",
                    "from_service": "a",
                    "call_name": "send",
                    "to_service": "target",
                    "to_path": "/x",
                },
                {"id": "legacy", "call_name": "send", "to_service": "other", "to_path": "/y"},
            ],
        }
    )
    fact = HttpFact(caller="run", line=1, call_name="send", client_id="sdk")
    index = EndpointIndex(GraphSnapshot())
    assert "::target::" in resolve_http(fact, index, sm, "r", "a")[0]
    assert "::other::" in resolve_http(fact, index, sm, "r", "b")[0]
    fact.client_id = "other-sdk"
    assert "::other::" in resolve_http(fact, index, sm, "r", "a")[0]


@pytest.mark.parametrize(
    "correction",
    [
        (
            "links: [{id: route, call_name: get, url_contains: /remote, "
            "to_service: remote, to_method: GET, to_path: /remote}]"
        ),
        "suppress: [{id: skip, from_api: 'GET /local', to_service: remote, reason: false edge}]",
        (
            "calls: [{id: call, from_api: 'GET /local', "
            "to: {service: remote, method: GET, path: /remote}, "
            "evidence: {file: app/main.py, line: 5, text: requests.get}}]"
        ),
        "endpoints: [{id: extra, method: GET, path: /extra, handler: helper0}]",
        "handlers: [{id: corrected, api: 'GET /local', handler: helper0}]",
    ],
)
def test_each_manual_config_invalidates_state_and_preserves_incremental_parity(
    tmp_path, correction
):
    import yaml

    from axon.v2.incremental import index_and_publish
    from axon.v2.store import MemoryStore
    from tests.v2.test_incremental import canonical, commit, fixture_repo

    root = tmp_path / "repo"
    root.mkdir()
    first = fixture_repo(root)
    store = MemoryStore()
    state = tmp_path / "state"
    index_and_publish(root, store, commit=first, state_dir=state)
    path = root / "axon.yaml"
    data = yaml.safe_load(path.read_text())
    data["services"][0].update(yaml.safe_load(correction))
    path.write_text(yaml.safe_dump(data))
    second = commit(root)
    snap, _, report = index_and_publish(root, store, commit=second, state_dir=state)
    assert report["mode"] == "full"
    full, _ = index_repo(root, commit=second, use_cache=False, workers=1)
    assert canonical(snap) == canonical(full)
    source = root / "app/module9.py"
    source.write_text(source.read_text().replace("pass", "return 42"))
    third = commit(root)
    snap, _, report = index_and_publish(root, store, commit=third, state_dir=state)
    assert report["mode"] == "incremental"
    full, _ = index_repo(root, commit=third, use_cache=False, workers=1)
    assert canonical(snap) == canonical(full)


def test_doctor_stale_link_and_redundant_native_endpoint(tmp_path):
    from axon.v2.doctor import doctor

    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text(
        "from fastapi import FastAPI\napp=FastAPI()\n@app.get('/native')\ndef run(): pass\n"
    )
    (tmp_path / "axon.yaml").write_text("""repo: r
services:
- id: app
  roots: [app]
  links:
  - {id: stale, call_name: removed_sdk, to_service: app, to_path: /native}
  endpoints:
  - {id: redundant, method: GET, path: /native, handler: run}
""")
    codes = {i["code"] for i in doctor(tmp_path)["issues"]}
    assert {"unmatched-link", "redundant-endpoint"} <= codes
