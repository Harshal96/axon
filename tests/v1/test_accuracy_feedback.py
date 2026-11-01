"""Generic, hand-authored contracts for extraction and conservative linking."""

import pytest

from axon.v1.facts import normalize_path
from axon.v1.linker import url_path
from axon.v1.parse_java import parse_java
from axon.v1.parse_python import parse_python


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/x/{attr:self.org}/y", "/x/{attr:self.org}/y"),
        ("/x/:id", "/x/{id}"),
        ("/x/*rest", "/x/{rest}"),
        ("/x/{id}:cancel", "/x/{id}:cancel"),
        ("/x/<int:id>", "/x/{id}"),
    ],
)
def test_opaque_route_parameters(value, expected):
    assert normalize_path(value) == expected


def test_symbolic_parameters_in_call_path():
    assert url_path("{env:BASE}/x/{attr:self.org}/{env:NAME}") == "/x/{var}/{var}"


@pytest.mark.parametrize(
    "mount",
    [
        "urlpatterns = router.urls",
        "urlpatterns = []; urlpatterns += router.urls",
        "urlpatterns = []; urlpatterns.extend(router.urls)",
        "urlpatterns = [*router.urls]",
        "urls = router.urls; urlpatterns = [*urls]",
        "urlpatterns = [router.urls]",
    ],
)
def test_router_assignment_mounts(mount):
    f = parse_python(
        "urls.py",
        "from rest_framework.routers import SimpleRouter\n"
        "router = SimpleRouter()\nrouter.register('items', ItemViewSet)\n" + mount,
    )
    assert any(i.kind == "drf" and i.module == "router" for i in f.includes)


def test_java_mapping_expression_arrays_and_partial():
    f = parse_java(
        "Controller.java",
        """
class Controller {
 static final String ROOT = "/v1";
 @GetMapping(path = {ROOT + "/items", ROOT + "/jobs"})
 public void read() {}
 @PostMapping("/items/" + UNKNOWN + "/run")
 public void run() {}
}
""",
    )
    assert {(r.method, r.path) for r in f.routes} == {
        ("GET", "/v1/items"),
        ("GET", "/v1/jobs"),
        ("POST", "/items/{unresolved}/run"),
    }
    assert any(d.category == "route-path-partial" for d in f.diagnostics)


@pytest.mark.parametrize(
    "transport",
    [
        'localVarRequestBuilder.method("POST", body);',
        'localVarApiClient.buildCall(base, localVarPath, "POST", args);',
        "apiClient.invokeAPI(localVarPath, HttpMethod.POST, args);",
    ],
)
def test_java_generated_transport(transport):
    f = parse_java(
        "ItemsApi.java",
        """class ItemsApi { public void add() {
 String localVarPath = "/items/{id}".replace("{id}", escape(id));
 """
        + transport
        + "}}",
    )
    assert [(h.method, h.url, h.via) for h in f.http_calls] == [
        ("POST", "/items/{id}", "generated-source")
    ]


def test_feign_request_line():
    f = parse_java(
        "ItemsApi.java",
        """interface ItemsApi { @RequestLine("GET /items/{id}") public Item read(); }""",
    )
    assert [(h.method, h.url, h.caller) for h in f.http_calls] == [
        ("GET", "/items/{id}", "ItemsApi.read")
    ]


def test_no_name_only_client_or_non_url_fact():
    f = parse_python(
        "app.py",
        """def run():
 client.execute_query("SELECT * FROM items")
 client.something("resource-name")
 client.get("https://example.test/items")
""",
    )
    assert [(h.method, h.url) for h in f.http_calls] == [("GET", "https://example.test/items")]
    assert any("untyped client receivers" in d.message for d in f.diagnostics)


def test_sdk_preserves_provider_without_url():
    f = parse_python(
        "app.py",
        """import boto3
s3 = boto3.client("s3")
def run():
 s3.get_object(Bucket="bucket", Key="key")
""",
    )
    assert [(h.via, h.provider, h.url) for h in f.http_calls] == [("sdk", "aws.s3", "")]


def test_cross_file_java_constants_resolve_without_truncation():
    from axon.v1.java_constants import resolve_constants

    const = parse_java(
        "Paths.java",
        """package common;
interface Paths { String ROOT = "/v3"; }
""",
    )
    routes = parse_java(
        "Controller.java",
        """import static common.Paths.ROOT;
class Controller { @GetMapping(ROOT + "/items") public void list() {} }
""",
    )
    resolve_constants([const, routes])
    assert routes.routes[0].path == "/v3/items"
    assert not routes.diagnostics


def test_warning_categories_are_incremental_evidence():
    from axon.v1.facts import ParseDiagnostic

    assert ParseDiagnostic(1, "SyntaxWarning", "invalid escape").severity == "informational"
    assert ParseDiagnostic(1, "SyntaxError", "invalid syntax").severity == "file-degraded"


def test_java_return_value_is_model_not_client():
    from axon.v1.indexer import _CallResolver

    f = parse_java(
        "Caller.java",
        """import generated.api.ItemsApi;
import generated.model.Item;
class Caller { public void run(ItemsApi api) { Item result = api.read(); result.getConfig(); } }
""",
    )
    api = parse_java(
        "ItemsApi.java",
        """package generated.api;
import generated.model.Item;
class ItemsApi { public Item read() { return null; } }
""",
    )
    resolver = _CallResolver(
        [f, api], {"Caller.java": ("service", "app"), "ItemsApi.java": ("client", "app")}
    )
    types = resolver.receiver_types("result", "Caller.java", "Caller.run")
    assert "generated.model.Item" in types
    assert not any(t.startswith("generated.api.") for t in types)


def test_catchall_metadata_survives_framework_normalization():
    from axon.v1.parse_go import parse_go

    go = parse_go(
        "main.go",
        """package main
func run() { router.GET("/v1/*rest", handler) }
""",
    )
    py = parse_python(
        "urls.py", "from django.urls import path\nurlpatterns = [path('v1/<path:rest>', view)]"
    )
    assert go.routes[0].catch_all
    assert py.routes[0].catch_all


def test_java_suffix_variants():
    from axon.v1.linker import _operation_base

    assert _operation_base("collectEntriesWithHttpInfoAsync") == "collectEntries"
    assert _operation_base("read_items_with_http_info") == "read_items"


def _write_repo(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.mark.parametrize("with_source", [False, True])
def test_deduplicated_operation_prefers_indexed_literal_identity(tmp_path, with_source):
    from axon.v1.indexer import index_repo
    from axon.v1.models import NodeKind

    _write_repo(
        tmp_path,
        {
            "axon.yaml": """repo: sample
services:
 - id: app
   roots: [app]
   clients:
    - id: remote-cli
      packages: [generated.api]
      paths: [generated]
      target_service: remote
 - id: remote
   roots: [remote]
   specs: [remote/openapi.yaml]
 - id: other
   roots: [other]
""",
            "app/main.py": """from fastapi import FastAPI
from generated.api import ItemsApi
api = ItemsApi()
app = FastAPI()
@app.get('/local')
def run(): return api.read_item_0()
""",
            "remote/main.py": """from fastapi import FastAPI
app = FastAPI()
@app.get('/items-a', operation_id='read_item')
def first(): pass
@app.get('/items-b', operation_id='read_item')
def second(): pass
""",
            "remote/openapi.yaml": """openapi: 3.0.0
info: {title: Remote, version: '1'}
paths:
 /items-a:
  get: {operationId: read_item, responses: {'200': {description: OK}}}
 /items-b:
  get: {operationId: read_item, responses: {'200': {description: OK}}}
""",
            "other/main.py": """from fastapi import FastAPI
app = FastAPI()
@app.get('/items-a')
def other(): pass
""",
        },
    )
    if with_source:
        _write_repo(
            tmp_path,
            {
                "generated/api.py": """class ItemsApi:
    def read_item_0(self):
        return self.api_client.call_api('/items-a', 'GET')
"""
            },
        )
    snap, _ = index_repo(tmp_path, use_cache=False, workers=1)
    local = next(n for n in snap.by_kind(NodeKind.API_ENDPOINT) if n.service_id == "app")
    edges = snap.outbound_calls(local.id)
    assert len(edges) == 1
    edge, target = edges[0]
    if with_source:
        assert target.service_id == "remote"
        assert target.props["path"] == "/items-a"
    else:
        import json

        assert target.kind == NodeKind.EXTERNAL_API
        assert len(json.loads(edge.props["candidates"])) == 2
        assert edge.evidence.rule == "client:remote-cli(dedupe-suffix)"


def test_client_source_inside_owner_and_sdk_are_separate_dependencies(tmp_path):
    from axon.v1.doctor import doctor
    from axon.v1.indexer import index_repo
    from axon.v1.models import NodeKind

    _write_repo(
        tmp_path,
        {
            "axon.yaml": """repo: sample
services:
 - id: app
   roots: [app]
   clients:
    - id: remote-cli
      packages: [samplecli]
      paths: []
      target_service: remote
 - id: remote
   roots: [remote]
""",
            "app/samplecli/api_client.py": "class ApiClient: pass\n",
            "app/samplecli/api.py": """class ItemsApi:
 def read_items(self):
  self.api_client.select_header_accept(['application/json'])
  return self.api_client.call_api('/items', 'GET')
""",
            "app/main.py": """from fastapi import FastAPI
from samplecli.api import ItemsApi
import boto3
app = FastAPI()
api = ItemsApi()
s3 = boto3.client('s3')
@app.get('/local')
def run():
 api.read_items()
 s3.get_object(Bucket='b', Key='k')
""",
            "remote/main.py": """from fastapi import FastAPI
app = FastAPI()
@app.get('/items', operation_id='read_items')
def read_items(): pass
""",
        },
    )
    snap, sm = index_repo(tmp_path, use_cache=False, workers=1)
    assert sm.services[0].clients[0].paths == ["app/samplecli"]
    assert not snap.unresolved
    assert len(snap.by_kind(NodeKind.EXTERNAL_API)) == 1
    assert snap.by_kind(NodeKind.EXTERNAL_API)[0].props["kind"] == "sdk"
    coverage = snap.coverage_by_service("sample")["app"]
    assert coverage["outbound_external"] == 0
    assert coverage["external_dependencies"] == 1
    assert any(i["code"] == "client-source-paths" for i in doctor(tmp_path)["issues"])


def test_generated_transport_concatenation_never_truncates():
    f = parse_java(
        "Api.java",
        """class Api { public void read() {
 String localVarPath = "/v1/" + "items";
 localVarRequestBuilder.GET();
}}
""",
    )
    assert [h.url for h in f.http_calls] == ["/v1/items"]


def test_parser_dispatch_truncation_is_graph_unsound():
    methods = "\n".join(f" def handle_{i}(self): pass" for i in range(26))
    f = parse_python(
        "app.py",
        "class Dispatcher:\n def run(self, name): getattr(self, f'handle_{name}')()\n" + methods,
    )
    assert any(d.severity == "graph-unsound" for d in f.diagnostics)


def test_java_annotation_comments_are_lexically_ignored():
    f = parse_java(
        "Controller.java",
        """class Controller {
 @GetMapping("/x/" /* explanation */ + "items") public void read() {}
}
""",
    )
    assert f.routes[0].path == "/x/items"


def test_transport_postfix_preserves_following_concatenation():
    f = parse_java(
        "Api.java",
        """class Api { public void read() {
 String localVarPath = "/items/{id}".replace("{id}", escape(id)) + "/create";
 localVarRequestBuilder.method("POST", body);
}}
""",
    )
    assert [h.url for h in f.http_calls] == ["/items/{id}/create"]


def test_java_inherited_interface_constant():
    f = parse_java(
        "Controller.java",
        """interface Paths { String ROOT = "/v1"; }
class Controller implements Paths {
 @GetMapping(ROOT + "/items") public void read() {}
}
""",
    )
    assert f.routes[0].path == "/v1/items"


def test_uri_builder_appended_path_is_never_silently_dropped():
    f = parse_java(
        "Api.java",
        """class Api { public void read() {
 String localVarPath = UriComponentsBuilder.fromPath("/items").path("/create").toUriString();
 apiClient.invokeAPI(localVarPath, HttpMethod.POST, args);
}}
""",
    )
    assert not f.http_calls
    assert any(d.category == "transport-path-partial" for d in f.diagnostics)
