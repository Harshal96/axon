import pytest

from axon.v1.parse_javascript import parse_javascript

pytestmark = pytest.mark.tier1


def test_route_handlers_and_calls_use_lexical_evidence():
    facts = parse_javascript(
        "api.ts",
        """import makeApp from 'express';
const app = makeApp();
app.get('/items/:id', (req, res) => res.json(load(req.params.id)));
app.post('/items', save);
const cache = new Map();
cache.get('/not-a-route', save);
function fake(app, load) { app.get('/fake', save); load(); }
function load(id: string) { return id; }
function save() {}
""",
    )
    assert facts.language == "typescript"
    assert [(r.method, r.path, r.line) for r in facts.routes] == [
        ("GET", "/items/{id}", 3),
        ("POST", "/items", 4),
    ]
    inline, named = facts.routes
    assert named.handler == "save"
    assert {s.qualified for s in facts.symbols} >= {inline.handler, "save", "load"}
    assert [(c.caller, c.called, c.line) for c in facts.calls] == [(inline.handler, "load", 3)]


def test_mounted_and_unmounted_commonjs_routers():
    from axon.v1.indexer import _effective_routes

    facts = parse_javascript(
        "api.cjs",
        """const express = require('express');
const server = express();
const router = express.Router();
const unused = express.Router();
const handler = () => {};
server.use('/v1', router);
router.route('/accounts').get(handler).patch(handler);
unused.get('/not-mounted', handler);
""",
    )
    assert [(r.method, r.path, r.handler) for r in facts.routes] == [
        ("GET", "/accounts", "handler"),
        ("PATCH", "/accounts", "handler"),
    ]
    assert len(facts.includes) == 1
    assert facts.includes[0].prefix == "/v1"
    assert facts.includes[0].symbol == facts.routes[0].router
    assert any("mount" in d.message.lower() for d in facts.diagnostics)
    assert [(r.method, r.path) for r in _effective_routes([facts])["api.cjs"]] == [
        ("GET", "/v1/accounts"),
        ("PATCH", "/v1/accounts"),
    ]


def test_typescript_jsx_and_source_visible_class_and_nested_calls():
    facts = parse_javascript(
        "component.tsx",
        """class Service {
  read() { return this.lookup(); }
  lookup() { return <span/>; }
}
const first = () => {
  const run = () => <p/>;
  return run();
};
const second = () => {
  function run() { return null; }
  return run();
};
""",
    )
    assert not facts.diagnostics
    assert {"Service.read", "Service.lookup", "first.run", "second.run"} <= {
        s.qualified for s in facts.symbols
    }
    assert {(c.caller, c.called) for c in facts.calls} == {
        ("Service.read", "Service.lookup"),
        ("first", "first.run"),
        ("second", "second.run"),
    }


def test_syntax_error_is_reported_at_source_line():
    facts = parse_javascript("bad.js", "const ok = 1;\nconst y = ;\n")
    assert [(d.severity, d.line) for d in facts.diagnostics] == [("file-degraded", 2)]


def test_module_level_transport_and_separate_callback_owners():
    facts = parse_javascript(
        "bootstrap.mjs",
        """import axios from "axios";
axios.get('/startup');
export function install() {
  const later = () => axios.get('/later');
  return later;
}
""",
    )
    assert {(f.url, f.caller) for f in facts.http_calls} == {
        ("/startup", "<module>"),
        ("/later", "install.later"),
    }
    assert "<module>" in {s.qualified for s in facts.symbols}
    assert facts.calls == []


def test_imported_route_handler_reaches_outbound_in_its_own_file(tmp_path):
    from axon.v1.indexer import index_repo
    from axon.v1.models import NodeKind

    (tmp_path / "axon.yaml").write_text("repo: test\nservices: [{id: app, roots: [src]}]\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "routes.js").write_text(
        "import express from 'express';\n"
        "import {show as showItem} from './items.js';\n"
        "const app = express();\n"
        "app.get('/items', showItem);\n"
        "function show() { return null; }\n"
    )
    (src / "items.js").write_text(
        "import axios from 'axios';\nexport function show() { return axios.get('/catalog'); }\n"
    )
    graph, _ = index_repo(tmp_path, workers=1, use_cache=False)
    (route,) = graph.by_kind(NodeKind.API_ENDPOINT)
    assert [(e.evidence.file, e.props["url"]) for e, _ in graph.outbound_calls(route.id)] == [
        ("src/items.js", "/catalog")
    ]
