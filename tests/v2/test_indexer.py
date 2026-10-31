import pytest

from axon.v2.indexer import index_repo
from axon.v2.models import NodeKind

pytestmark = pytest.mark.tier1


def _eps(snap):
    return {
        (e.service_id, e.props["method"], e.props["path"])
        for e in snap.by_kind(NodeKind.API_ENDPOINT)
    }


def test_monorepo_fastapi_and_spec_reconciled(monorepo_snap):
    eps = _eps(monorepo_snap)
    assert ("payment", "POST", "/v1/charges") in eps
    assert ("payment", "GET", "/v1/charges/{charge_id}") in eps
    assert ("payment", "GET", "/v1/legacy") in eps  # spec-only
    assert ("checkout", "POST", "/checkout") in eps
    charge = next(
        e
        for e in monorepo_snap.by_kind(NodeKind.API_ENDPOINT)
        if e.props["path"] == "/v1/charges" and e.props["method"] == "POST"
    )
    assert charge.props["source"] == "both"
    handler = monorepo_snap.handler_of(charge.id)
    assert handler is not None and handler.name == "create_charge"


def test_disconnected_generated_client_is_linked(monorepo_snap):
    checkout = next(
        e for e in monorepo_snap.by_kind(NodeKind.API_ENDPOINT) if e.props["path"] == "/checkout"
    )
    targets = {t.id for t in monorepo_snap.outbound_endpoints(checkout.id)}
    assert any("POST:/v1/charges" in t for t in targets)
    orders = next(
        e
        for e in monorepo_snap.by_kind(NodeKind.API_ENDPOINT)
        if e.props["path"] == "/checkout/{order_id}/status"
    )
    targets = {t.props.get("path") for t in monorepo_snap.outbound_endpoints(orders.id)}
    assert "/v1/charges/{charge_id}" in targets


def test_django_routes(fixtures):
    snap, _ = index_repo(str(fixtures / "django_app"))
    eps = _eps(snap)
    assert ("shop", "ANY", "/orders") in eps
    assert ("shop", "ANY", "/orders/{order_id}") in eps
    ep = next(
        e for e in snap.by_kind(NodeKind.API_ENDPOINT) if e.props["path"] == "/orders/{order_id}"
    )
    handler = snap.handler_of(ep.id)
    assert handler is not None and handler.name == "get_order"
    assert "load_order" in {snap.nodes[s].name for s in snap.call_closure(handler.id)}


def test_spring_routes_and_external_call(fixtures):
    snap, _ = index_repo(str(fixtures / "spring_app"))
    eps = _eps(snap)
    assert ("billing", "POST", "/v1/charges") in eps
    assert ("billing", "GET", "/v1/charges/{id}") in eps
    ep = next(e for e in snap.by_kind(NodeKind.API_ENDPOINT) if e.props["method"] == "POST")
    out = snap.outbound_endpoints(ep.id)
    assert out and out[0].kind == NodeKind.EXTERNAL_API
    assert snap.unresolved  # ledger service is not indexed


def test_go_cross_service_call(fixtures):
    snap, _ = index_repo(str(fixtures / "go_app"))
    eps = _eps(snap)
    assert ("orders", "POST", "/orders") in eps
    assert ("payments", "POST", "/v1/charges") in eps
    orders = next(
        e
        for e in snap.by_kind(NodeKind.API_ENDPOINT)
        if e.service_id == "orders" and e.props["method"] == "POST"
    )
    targets = {t.service_id for t in snap.outbound_endpoints(orders.id)}
    assert targets == {"payments"}


def test_vertx_router_and_webclient(fixtures):
    snap, _ = index_repo(str(fixtures / "vertx_app"))
    eps = _eps(snap)
    assert ("gateway", "POST", "/v1/charges") in eps
    assert ("gateway", "GET", "/health") in eps
    ep = next(e for e in snap.by_kind(NodeKind.API_ENDPOINT) if e.props["method"] == "POST")
    handler = snap.handler_of(ep.id)
    assert handler is not None and handler.name == "createCharge"
    out = snap.outbound_endpoints(ep.id)
    assert out and out[0].kind == NodeKind.EXTERNAL_API


def test_top_level_venv_and_worktrees_are_not_indexed(tmp_path):
    from axon.v2.servicemap import ServiceMap, is_ignored

    sm = ServiceMap(repo="r")
    assert is_ignored(sm, ".venv/lib/site.py")
    assert is_ignored(sm, "node_modules/pkg/index.py")
    assert is_ignored(sm, ".worktrees/wip/svc/app.py")

    (tmp_path / "axon.yaml").write_text(
        "version: 1\nrepo: r\nservices:\n  - id: svc\n    roots: [svc]\n    language: python\n",
        encoding="utf-8",
    )
    for rel in ("svc/app.py", ".venv/lib/fake.py", ".worktrees/w/svc/app.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def f():\n    return 1\n", encoding="utf-8")

    snap, _ = index_repo(str(tmp_path))
    files = {n.file for n in snap.by_kind(NodeKind.SOURCE_FILE)}
    assert files == {"svc/app.py"}


def test_parallel_index_matches_serial(fixtures):
    serial, _ = index_repo(str(fixtures / "monorepo"), workers=1)
    parallel, _ = index_repo(str(fixtures / "monorepo"), workers=2)

    def fingerprint(snap):
        nodes = sorted((n.id, n.kind.value) for n in snap.nodes.values())
        edges = sorted((e.from_id, e.to_id, e.kind.value, e.evidence.line) for e in snap.edges)
        return nodes, edges, sorted(snap.unresolved)

    assert fingerprint(parallel) == fingerprint(serial)


def test_many_cross_calls_resolve_via_suffix_index(tmp_path):
    import time

    svc = tmp_path / "svc"
    svc.mkdir()
    (tmp_path / "axon.yaml").write_text(
        "version: 1\nrepo: r\nservices:\n  - id: svc\n    roots: [svc]\n    language: python\n",
        encoding="utf-8",
    )
    for f in range(30):
        body = "".join(
            f"def fn_{f}_{k}(x):\n    return fn_{(f + 1) % 30}_{k}(x)\n" for k in range(40)
        )
        (svc / f"mod{f}.py").write_text(body, encoding="utf-8")

    started = time.perf_counter()
    snap, _ = index_repo(str(tmp_path))
    assert time.perf_counter() - started < 20  # old per-call full scan took far longer
    calls = [e for e in snap.edges if e.kind.value == "CALLS"]
    assert len(calls) >= 1200


def _mini_repo(tmp_path, files: dict[str, str], extra_yaml: str = ""):
    (tmp_path / "axon.yaml").write_text(
        "version: 1\nrepo: r\nservices:\n  - id: svc\n    roots: [svc]\n"
        "    language: python\n" + extra_yaml,
        encoding="utf-8",
    )
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_index_timings_are_filled(fixtures):
    from axon.v2.indexer import IndexTimings

    timings = IndexTimings()
    index_repo(str(fixtures / "monorepo"), timings=timings, use_cache=False)
    assert timings.total_s > 0
    assert timings.files_total > 0
    assert timings.files_parsed == timings.files_total
    assert timings.files_cached == 0


def test_second_index_uses_facts_cache(tmp_path, monkeypatch):
    import axon.v2.indexer as indexer

    repo = _mini_repo(
        tmp_path,
        {"svc/a.py": "def f():\n    return g()\n", "svc/b.py": "def g():\n    return 1\n"},
    )
    monkeypatch.setenv("AXON_FACTS_CACHE_DIR", str(tmp_path / "cache"))
    from axon.v2.indexer import IndexTimings

    first = IndexTimings()
    snap1, _ = index_repo(str(repo), workers=1, timings=first)
    assert first.files_parsed == 2 and first.files_cached == 0

    def boom(rel, text):
        raise AssertionError("parser must not run on a warm cache")

    monkeypatch.setitem(indexer.PARSERS, ".py", boom)
    second = IndexTimings()
    snap2, _ = index_repo(str(repo), workers=1, timings=second)
    assert second.files_cached == 2 and second.files_parsed == 0
    assert {n.id for n in snap2.nodes.values()} == {n.id for n in snap1.nodes.values()}


def test_cache_shared_across_paths_and_disabled_by_env(tmp_path, monkeypatch):
    repo = _mini_repo(
        tmp_path,
        {"svc/a.py": "def same():\n    return 1\n", "svc/b.py": "def same():\n    return 1\n"},
    )
    monkeypatch.setenv("AXON_FACTS_CACHE_DIR", str(tmp_path / "cache"))
    snap, _ = index_repo(str(repo), workers=1)
    files = {n.file for n in snap.by_kind(NodeKind.SOURCE_FILE)}
    assert files == {"svc/a.py", "svc/b.py"}

    monkeypatch.setenv("AXON_FACTS_CACHE", "0")
    index_repo(str(repo), workers=1)
    cache_files = (
        list((tmp_path / "cache").rglob("*.json")) if (tmp_path / "cache").exists() else []
    )
    before = len(cache_files)
    (tmp_path / "svc/c.py").write_text("def other():\n    return 2\n", encoding="utf-8")
    index_repo(str(repo), workers=1)
    after = list((tmp_path / "cache").rglob("*.json"))
    assert len(after) == before


def test_scope_policy_knobs(tmp_path):
    repo = _mini_repo(
        tmp_path,
        {
            "svc/app.py": "def prod():\n    return 1\n",
            "svc/test_app.py": "def test_prod():\n    return 1\n",
            "outside/util.py": "def loose():\n    return 1\n",
        },
    )
    names = {n.name for n in index_repo(str(repo), use_cache=False)[0].by_kind(NodeKind.SYMBOL)}
    # Defaults: test files are excluded, unowned files are included.
    assert {"prod", "loose"} <= names
    assert "test_prod" not in names

    _mini_repo(
        tmp_path,
        {
            "svc/app.py": "def prod():\n    return 1\n",
            "svc/test_app.py": "def test_prod():\n    return 1\n",
            "outside/util.py": "def loose():\n    return 1\n",
        },
        extra_yaml="index:\n  exclude_tests: false\n",
    )
    names = {n.name for n in index_repo(str(repo), use_cache=False)[0].by_kind(NodeKind.SYMBOL)}
    assert {"prod", "test_prod", "loose"} <= names

    _mini_repo(
        tmp_path,
        {
            "svc/app.py": "def prod():\n    return 1\n",
            "svc/test_app.py": "def test_prod():\n    return 1\n",
            "outside/util.py": "def loose():\n    return 1\n",
        },
        extra_yaml="index:\n  include_unowned: false\n  exclude_tests: true\n",
    )
    names = {n.name for n in index_repo(str(repo), use_cache=False)[0].by_kind(NodeKind.SYMBOL)}
    assert names == {"prod"}


def test_is_test_path_predicate():
    from axon.v2.indexer import _is_test_path

    for rel in (
        "a/tests/x.py",
        "svc/test_app.py",
        "a/b_test.go",
        "src/test/java/F.java",
        "FTest.java",
        "svc/conftest.py",
        "svc/util_tests.py",
        "e2e/flow.py",
        "pkg/test_utils/helper.py",
        "FTestCase.java",
    ):
        assert _is_test_path(rel), rel
    for rel in ("svc/testing.py", "svc/contest.py", "svc/latest.py", "src/main/java/F.java"):
        assert not _is_test_path(rel), rel


def test_test_code_never_becomes_nodes_by_default(tmp_path):
    repo = _mini_repo(
        tmp_path,
        {
            "svc/app.py": "class Widget:\n    def render(self):\n        return 1\n\n"
            "def test_connection():\n    return True\n",
            "svc/test_widget.py": "class TestWidget:\n    def test_render(self):\n        pass\n",
            "svc/tests/helper.py": "def helper():\n    return 1\n",
            "svc/conftest.py": "def fixture_one():\n    return 1\n",
            "svc/WidgetTest.java": "class WidgetTest { void testIt() {} }\n",
            "svc/main_test.go": "package svc\nfunc TestMain() {}\n",
        },
    )
    snap, _ = index_repo(str(repo), use_cache=False)
    names = {n.name for n in snap.by_kind(NodeKind.SYMBOL)}
    files = {n.file for n in snap.by_kind(NodeKind.SOURCE_FILE)}
    test_names = {"TestWidget", "test_render", "helper", "fixture_one", "WidgetTest", "TestMain"}
    assert test_names.isdisjoint(names)
    assert files == {"svc/app.py"}
    # A test-named production symbol in a production file stays.
    assert {"Widget", "test_connection"} <= names


def test_progress_and_cancellation(fixtures):
    import threading

    from axon.v2.indexer import IndexCancelled

    calls: list[tuple[str, int, int]] = []
    index_repo(
        str(fixtures / "monorepo"),
        use_cache=False,
        progress=lambda p, d, t: calls.append((p, d, t)),
    )
    phases = [c[0] for c in calls]
    assert phases.index("parse") < phases.index("calls")
    parse_dones = [d for p, d, _ in calls if p == "parse"]
    assert parse_dones == sorted(parse_dones)

    event = threading.Event()

    def set_on_symbols(phase, done, total):
        if phase == "symbols":
            event.set()

    with pytest.raises(IndexCancelled):
        index_repo(
            str(fixtures / "monorepo"),
            use_cache=False,
            progress=set_on_symbols,
            cancelled=event,
        )


def test_index_aggregates_parse_diagnostics(tmp_path):
    repo = _mini_repo(
        tmp_path,
        {
            "svc/a.py": "PAT = '\\d'\nPAT2 = '\\s'\n\ndef f():\n    return 1\n",
            "svc/b.py": "def g():\n    return 1\n",
        },
    )
    snap, _ = index_repo(str(repo), workers=1, use_cache=False)
    assert len(snap.diagnostics) == 2
    assert snap.diagnostics[0].startswith("svc/a.py:1: invalid escape sequence")
    assert snap.diagnostics[1].startswith("svc/a.py:2: invalid escape sequence")


def test_index_no_stderr_noise_under_pool(tmp_path, capfd):
    repo = _mini_repo(
        tmp_path,
        {
            "svc/a.py": "PAT = '\\d'\n\ndef f():\n    return 1\n",
            "svc/b.py": "PAT = '\\s'\n\ndef g():\n    return 1\n",
        },
    )
    serial, _ = index_repo(str(repo), workers=1, use_cache=False)
    parallel, _ = index_repo(str(repo), workers=2, use_cache=False)
    err = capfd.readouterr().err
    assert "SyntaxWarning" not in err and "DeprecationWarning" not in err
    assert parallel.diagnostics == serial.diagnostics


def _two_service_repo(tmp_path, client_yaml: str, view_body: str):
    (tmp_path / "axon.yaml").write_text(
        "version: 1\nrepo: r\n"
        "services:\n"
        "  - id: consumer\n    roots: [consumer]\n    language: python\n    framework: fastapi\n"
        + client_yaml
        + "  - id: payments\n    roots: [payments]\n    language: python\n    framework: fastapi\n"
        "shared: [libs]\n",
        encoding="utf-8",
    )

    def write_rel(rel: str, text: str) -> None:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    write_rel(
        "payments/app.py",
        "from fastapi import FastAPI\napp = FastAPI()\n\n"
        "@app.get('/api/v1/payments', operation_id='v1_payment_list')\n"
        "def list_payments():\n    return []\n",
    )
    write_rel(
        "libs/retry_utils.py",
        "def _loop(fn):\n    return fn()\n\n"
        "def call_with_retry(fn):\n    return _loop(lambda: fn())\n\n"
        "def call_payments_api(fn):\n    return call_with_retry(fn)\n",
    )
    write_rel("consumer/app.py", view_body)
    return tmp_path


_CLIENT_YAML = (
    "    clients:\n      - id: paymentscli\n        packages: [paymentscli]\n"
    "        factories: [settings.GET_PAYMENTS_API]\n        target_service: payments\n"
)
_VIEW = (
    "import settings\nfrom fastapi import FastAPI\nfrom retry_utils import call_payments_api\n"
    "app = FastAPI()\n\n@app.get('/orders')\ndef view():\n"
    "    api = settings.GET_PAYMENTS_API()\n    return call_payments_api(api.v1_payment_list)\n"
)


def _calls_api_targets(snap):
    return {
        (snap.nodes[e.from_id].name, snap.nodes[e.to_id].name)
        for e in snap.edges
        if e.kind.value == "CALLS_API"
    }


def test_factory_client_without_generated_source_resolves(tmp_path):
    # pip-installed client: no generated symbols on disk; the declared
    # factory + operationId still produce the edge.
    repo = _two_service_repo(tmp_path, _CLIENT_YAML, _VIEW)
    snap, _ = index_repo(str(repo), use_cache=False)
    assert ("view", "GET /api/v1/payments") in _calls_api_targets(snap)


def test_undeclared_factory_stays_unresolved(tmp_path):
    repo = _two_service_repo(tmp_path, "", _VIEW)
    snap, _ = index_repo(str(repo), use_cache=False)
    assert ("view", "GET /api/v1/payments") not in _calls_api_targets(snap)


def test_call_through_synthesis_at_scale():
    # Regression: resolve() used to scan every symbol per call, which
    # hung large repos in this stage. Build the symbol table directly
    # and check the wrapper chain still resolves through the noise.
    import threading

    from axon.v2.facts import CallFact, FileFacts, SymFact
    from axon.v2.indexer import IndexCancelled, _synthesize_call_throughs
    from axon.v2.servicemap import ServiceMap

    facts = FileFacts(path="svc/app.py", language="python")
    for i in range(30_000):
        facts.symbols.append(
            SymFact(
                qualified=f"pkg{i}.mod{i}.fn{i}",
                name=f"fn{i}",
                kind="FUNCTION",
                line_start=1,
                line_end=1,
                params=["x"],
            )
        )
        if i < 3_000:  # call traffic: every call used to trigger a full scan
            facts.calls.append(CallFact(f"pkg{i}.mod{i}.fn{i}", f"ext{i}.run{i}", 1))
    facts.symbols += [
        SymFact("wrap", "wrap", "FUNCTION", 1, 1, params=["fn"]),
        SymFact("target", "target", "FUNCTION", 1, 1),
        SymFact("view", "view", "FUNCTION", 1, 1),
    ]
    facts.calls += [
        CallFact("wrap", "fn", 1),
        CallFact("view", "wrap", 2, callable_args=[(0, "target")]),
    ]
    _synthesize_call_throughs([facts], ServiceMap(repo="r"))
    assert CallFact("view", "target", 2) in facts.calls

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(IndexCancelled):
        _synthesize_call_throughs([facts], ServiceMap(repo="r"), cancelled)


def test_django_cbv_merges_onto_spec_endpoint(tmp_path):
    repo = _mini_repo(
        tmp_path,
        {
            "svc/swagger.json": (
                '{"openapi":"3.0.0","info":{"title":"t","version":"1"},'
                '"paths":{"/v1/items/{item_id}":{"put":{"operationId":"itemUpsert",'
                '"responses":{"200":{"description":"ok"}}}}}}'
            ),
            "svc/urls.py": (
                "from django.urls import path\nfrom views import ItemView\n"
                "urlpatterns = [path('v1/items/<uuid:item_id>', ItemView.as_view())]\n"
            ),
            "svc/views.py": (
                "import requests\n\nclass ItemView:\n"
                "    def put(self, request, item_id):\n"
                "        return requests.get('https://example.com/x')\n"
            ),
        },
        extra_yaml="    framework: django\n    specs: [svc/swagger.json]\n",
    )
    snap, _ = index_repo(str(repo), use_cache=False)
    eps = snap.by_kind(NodeKind.API_ENDPOINT)
    assert [(e.props["method"], e.props["path"], e.props["source"]) for e in eps] == [
        ("PUT", "/v1/items/{item_id}", "both")
    ]
    handler = snap.handler_of(eps[0].id)
    assert handler is not None and handler.name == "put"
    assert [t.name for _, t in snap.outbound_calls(eps[0].id)] == ["GET https://example.com/x"]
    assert snap.coverage_by_service("r") == {
        "svc": {
            "endpoints": 1,
            "with_handler": 1,
            "outbound_resolved": 0,
            "outbound_external": 1,
        }
    }


def test_django_include_prefixes(tmp_path):
    repo = _mini_repo(
        tmp_path,
        {
            "svc/root_urls.py": (
                "from django.urls import include, path\n"
                "urlpatterns = [path('api/', include('svc.app_urls'))]\n"
            ),
            "svc/app_urls.py": (
                "from django.urls import path\nfrom views import listing\n"
                "urlpatterns = [path('items/', listing)]\n"
            ),
            "svc/views.py": "def listing(request):\n    return None\n",
        },
        extra_yaml="    framework: django\n",
    )
    snap, _ = index_repo(str(repo), use_cache=False)
    eps = snap.by_kind(NodeKind.API_ENDPOINT)
    assert [(e.props["method"], e.props["path"]) for e in eps] == [("ANY", "/api/items")]
    handler = snap.handler_of(eps[0].id)
    assert handler is not None and handler.name == "listing"


def test_local_callable_pass_through_is_plain_calls(tmp_path):
    repo = _mini_repo(
        tmp_path,
        {
            "svc/app.py": "from retry_utils import call_with_retry\n\n"
            "def helper():\n    return 1\n\n"
            "def run():\n    return call_with_retry(helper)\n",
            "svc/retry_utils.py": "def call_with_retry(fn):\n    return fn()\n",
        },
    )
    snap, _ = index_repo(str(repo), use_cache=False)
    calls = {
        (snap.nodes[e.from_id].name, snap.nodes[e.to_id].name)
        for e in snap.edges
        if e.kind.value == "CALLS"
    }
    assert ("run", "helper") in calls
    assert _calls_api_targets(snap) == set()


def test_function_to_apis(monorepo_snap):
    price = monorepo_snap.find_symbols("price_cart")[0]
    apis = monorepo_snap.endpoint_of_symbol(price.id)
    assert [a.props["path"] for a in apis] == ["/checkout"]
    util = monorepo_snap.find_symbols("normalize_currency")[0]
    apis = monorepo_snap.endpoint_of_symbol(util.id)
    assert {a.props["path"] for a in apis} == {"/v1/charges"}
