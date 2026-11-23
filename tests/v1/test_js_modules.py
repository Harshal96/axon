from pathlib import Path

import pytest

from axon.v1.facts import facts_from_dict
from axon.v1.indexer import index_repo
from axon.v1.models import EdgeKind


@pytest.mark.parametrize("bad", [{"axios_exports": []}, {"js_import_calls": ["invalid"]}])
def test_invalid_cached_module_facts_have_recoverable_shape_errors(bad):
    with pytest.raises(ValueError):
        facts_from_dict({"path": "a.ts", "language": "typescript", **bad})


def write_service(root: Path, files: dict[str, str]) -> None:
    (root / "axon.yaml").write_text("repo: example\nservices: [{id: app, roots: [src]}]\n")
    for name, source in files.items():
        path = root / "src" / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(source)


def test_imported_clients_require_a_matching_runtime_export(tmp_path):
    write_service(
        tmp_path,
        {
            "client.ts": """
import axios from 'axios';
const catalog = axios.create({baseURL: 'https://catalog.example/v2', method: 'POST'});
export {catalog as stock};
export default axios.create({baseURL: 'https://books.example'});
export const cache = new Map();
""",
            "reader.ts": """
import books, {stock as inventory, cache} from './client.js';
export function read() {
  books.get('/item');
  inventory({url: '/new'});
  inventory.request({method: 'put', url: '/update', baseURL: 'https://override.example'});
  cache.get('/ignored');
}
export function shadow(books) { return books.get('/shadow'); }
""",
        },
    )
    graph, _ = index_repo(tmp_path, workers=1, use_cache=False)
    edges = [e for e in graph.edges if e.kind == EdgeKind.CALLS_API]
    assert {(e.props["method"], e.props["url"]) for e in edges} == {
        ("GET", "https://books.example/item"),
        ("POST", "https://catalog.example/v2/new"),
        ("PUT", "https://override.example/update"),
    }
    assert all(e.evidence.file == "src/reader.ts" for e in edges)
    assert all("src/client.ts:" in e.evidence.detail for e in edges)


def test_unproved_relative_imports_create_no_outbound_edges(tmp_path):
    write_service(
        tmp_path,
        {
            "west/client.ts": "import axios from 'axios'; export const api = axios.create();",
            "east/client.ts": "export const api = new Map();",
            "west/ambiguous.ts": "import axios from 'axios'; export default axios.create();",
            "west/ambiguous.js": "export default new Map();",
            "west/modified.ts": """
import axios from 'axios';
const api = axios.create();
api.get = () => 7;
export default api;
""",
            "west/both.ts": """
import axios from 'axios';
export const storage = axios.create();
export default {storage: new Map()};
""",
            "east/reader.ts": """
import {api} from './client';
import {api as missing} from './unknown';
import {api as alias} from '@app/west/client';
import wrapper from '../west/both';
import ambiguous from '../west/ambiguous.js';
import directory from '../west/ambiguous/';
import modified from '../west/modified';
export function read() {
  api.get('/map'); missing.get('/unknown'); alias.get('/alias'); wrapper.storage.get('/storage');
  ambiguous.get('/compiled'); directory.get('/directory'); modified.get('/overridden');
}
""",
        },
    )
    graph, _ = index_repo(tmp_path, workers=1, use_cache=False)
    assert not any(e.kind == EdgeKind.CALLS_API for e in graph.edges)


def test_incremental_export_change_updates_unchanged_importer(tmp_path):
    from axon.v1.incremental import index_and_publish
    from axon.v1.store import MemoryStore
    from tests.v1.test_incremental import canonical, commit, fixture_repo

    root = tmp_path / "repo"
    root.mkdir()
    fixture_repo(root)
    client = root / "remote" / "client.js"
    client.write_text("import axios from 'axios'; export default axios.create({baseURL: '/v1'});")
    (root / "app" / "reader.js").write_text(
        "import api from '../remote/client.js'; "
        "export function read() { return api.get('/items'); }"
    )
    store = MemoryStore()

    def urls():
        return [e.props["url"] for e in store.load().edges if e.props.get("via") == "axios"]

    index_and_publish(root, store, commit=commit(root), state_dir=tmp_path / "state")
    assert urls() == ["/v1/items"]
    for text, expected in (
        (client.read_text().replace("/v1", "/v2"), ["/v2/items"]),
        ("export default new Map();", []),
    ):
        client.write_text(text)
        revision = commit(root)
        snapshot, _, report = index_and_publish(
            root, store, commit=revision, state_dir=tmp_path / "state"
        )
        assert report["mode"] == "incremental"
        assert urls() == expected
        full, _ = index_repo(root, commit=revision, workers=1, use_cache=False)
        assert canonical(snapshot) == canonical(full)


def test_endpoint_reaches_imported_wrapper_and_drops_stale_calls_on_reindex(tmp_path):
    from axon.v1.incremental import index_and_publish
    from axon.v1.models import NodeKind
    from axon.v1.store import MemoryStore
    from tests.v1.test_incremental import canonical, commit, fixture_repo

    root = tmp_path / "repo"
    root.mkdir()
    fixture_repo(root)
    wrapper = root / "remote" / "catalog.client.ts"
    wrapper.write_text("""
import axios from 'axios';
function readItem() { return axios.get('/v1/items'); }
function readOther() { return axios.get('/v2/items'); }
export {readItem as read};
""")
    (root / "app" / "route.ts").write_text("""
import express from 'express';
import {read as fetchItem} from '../remote/catalog.client';
const app = express();
app.get('/inventory', () => fetchItem());
""")
    store = MemoryStore()

    def outbound():
        snap = store.load()
        route = next(
            n for n in snap.by_kind(NodeKind.API_ENDPOINT) if n.props["path"] == "/inventory"
        )
        return [edge.props["url"] for edge, _ in snap.outbound_calls(route.id)]

    index_and_publish(root, store, commit=commit(root), state_dir=tmp_path / "state")
    assert outbound() == ["/v1/items"]
    wrapper.write_text(
        wrapper.read_text().replace("export {readItem as read}", "export {readOther as read}")
    )
    revision = commit(root)
    snapshot, _, report = index_and_publish(
        root, store, commit=revision, state_dir=tmp_path / "state"
    )
    assert report["mode"] == "incremental"
    assert outbound() == ["/v2/items"]
    full, _ = index_repo(root, commit=revision, workers=1, use_cache=False)
    assert canonical(snapshot) == canonical(full)
