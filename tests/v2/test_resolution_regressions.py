"""Generic source-level resolution regressions."""

from axon.v2.indexer import index_repo
from axon.v2.models import EdgeKind, NodeKind


def _index(tmp_path, files):
    (tmp_path / "axon.yaml").write_text(
        "version: 1\n"
        "repo: sample\n"
        "services:\n"
        "  - id: app\n"
        "    roots: [app]\n"
        "    language: python\n"
        "  - id: other\n"
        "    roots: [other]\n"
        "    language: python\n"
        "shared: [libs]\n"
    )
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return index_repo(tmp_path, use_cache=False, workers=1)[0]


def _calls(snap, caller):
    return {
        (snap.nodes[e.to_id].file, snap.nodes[e.to_id].props["qualified"])
        for e in snap.edges
        if e.kind == EdgeKind.CALLS and snap.nodes[e.from_id].props.get("qualified") == caller
    }


def test_unknown_receiver_never_links_by_leaf(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": "def run():\n    return unknown.execute()\n",
            "app/other.py": "def execute():\n    pass\n",
        },
    )
    assert _calls(snap, "run") == set()


def test_bare_function_does_not_resolve_unrelated_method_or_service(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": "def run():\n    return execute()\n",
            "app/other.py": "class Worker:\n    def execute(self):\n        pass\n",
            "other/main.py": "def execute():\n    pass\n",
        },
    )
    assert _calls(snap, "run") == set()


def test_import_alias_resolves_shared_library_without_service_leak(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": (
                "from libs.worker import execute as perform\ndef run():\n    return perform()\n"
            ),
            "libs/worker.py": "def execute():\n    pass\n",
            "other/main.py": "def execute():\n    pass\n",
        },
    )
    assert _calls(snap, "run") == {("libs/worker.py", "execute")}


def test_constructor_binding_and_inherited_method(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": (
                "from libs.worker import Worker\n"
                "def run():\n"
                "    worker = Worker()\n"
                "    return worker.execute()\n"
            ),
            "libs/worker.py": (
                "class Base:\n    def execute(self):\n        pass\nclass Worker(Base):\n    pass\n"
            ),
        },
    )
    assert ("libs/worker.py", "Base.execute") in _calls(snap, "run")


def test_concrete_code_route_reconciles_spec_template_name(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": (
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/items/{key}')\n"
                "def lookup(key):\n"
                "    pass\n"
            ),
            "app/openapi.yaml": (
                "openapi: 3.0.0\n"
                'info: {title: Example, version: "1"}\n'
                "paths:\n"
                "  /items/{item_id}:\n"
                "    get: {operationId: lookup, responses: {}}\n"
            ),
        },
    )
    eps = snap.by_kind(NodeKind.API_ENDPOINT)
    assert len(eps) == 1
    assert eps[0].props["source"] == "both"


def test_fastapi_imported_router_mount_preserves_handler_file(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": (
                "from fastapi import FastAPI\n"
                "from app.routes import router\n"
                "app = FastAPI()\n"
                "app.include_router(router, prefix='/api')\n"
            ),
            "app/routes.py": (
                "from fastapi import APIRouter\n"
                "router = APIRouter(prefix='/v1')\n"
                "@router.get('/items')\n"
                "def listing():\n"
                "    pass\n"
            ),
        },
    )
    eps = snap.by_kind(NodeKind.API_ENDPOINT)
    assert [(e.props["method"], e.props["path"]) for e in eps] == [("GET", "/api/v1/items")]
    assert snap.handler_of(eps[0].id).file == "app/routes.py"


def test_django_short_include_chooses_nearest_module(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/root.py": (
                "from django.urls import path, include\n"
                "urlpatterns = [path('api/', include('urls'))]\n"
            ),
            "app/urls.py": (
                "from django.urls import path\n"
                "def local():\n"
                "    pass\n"
                "urlpatterns = [path('items/', local)]\n"
            ),
            "other/urls.py": (
                "from django.urls import path\n"
                "def foreign():\n"
                "    pass\n"
                "urlpatterns = [path('foreign/', foreign)]\n"
            ),
        },
    )
    assert ("app", "/api/items") in {
        (e.service_id, e.props["path"]) for e in snap.by_kind(NodeKind.API_ENDPOINT)
    }
    assert ("other", "/foreign") in {
        (e.service_id, e.props["path"]) for e in snap.by_kind(NodeKind.API_ENDPOINT)
    }


def test_repository_root_service_owns_root_and_nested_files():
    from axon.v2.servicemap import ServiceDecl, ServiceMap, owner_of

    sm = ServiceMap(
        repo="sample",
        services=[ServiceDecl(id="app", roots=["."]), ServiceDecl(id="nested", roots=["sub"])],
    )
    assert owner_of(sm, "main.py") == ("service", "app")
    assert owner_of(sm, "pkg/mod.py") == ("service", "app")
    assert owner_of(sm, "sub/mod.py") == ("service", "nested")


def test_spec_base_path_is_preserved_and_suffix_match_keeps_http_verb(tmp_path):
    from axon.v2.linker import EndpointIndex

    snap = _index(
        tmp_path,
        {
            "app/swagger.yaml": (
                'swagger: "2.0"\n'
                "basePath: /gateway/v1\n"
                'info: {title: Example, version: "1"}\n'
                "paths:\n"
                "  /items:\n"
                "    get: {operationId: listItems, responses: {}}\n"
                "    post: {operationId: createItems, responses: {}}\n"
            )
        },
    )
    assert {e.props["path"] for e in snap.by_kind(NodeKind.API_ENDPOINT)} == {"/gateway/v1/items"}
    hits = EndpointIndex(snap).by_method_path("GET", "/items")
    assert [(e.props["method"], e.props["path"]) for e in hits] == [("GET", "/gateway/v1/items")]


def test_java_factory_return_chain_links_final_method(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/Main.java": (
                "class Main {\n  public void run() {\n    Store.getInstance().update();\n  }\n}"
            ),
            "app/Store.java": (
                "class Store {\n"
                "  public static Store getInstance() {\n"
                "    return null;\n"
                "  }\n"
                "  public void update() {}\n"
                "}"
            ),
        },
    )
    assert ("app/Store.java", "Store.update") in _calls(snap, "Main.run")


def test_java_package_import_resolves_shared_field_type(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/src/main/java/com/example/Main.java": (
                "package com.example;\n"
                "import com.shared.Worker;\n"
                "class Main {\n"
                " private Worker worker;\n"
                " public void run() {\n"
                "  worker.execute();\n"
                " }\n"
                "}"
            ),
            "libs/src/main/java/com/shared/Worker.java": (
                "package com.shared;\nclass Worker {\n public void execute() {}\n}"
            ),
        },
    )
    assert ("libs/src/main/java/com/shared/Worker.java", "Worker.execute") in _calls(
        snap, "Main.run"
    )


def test_same_named_view_classes_do_not_share_handlers(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/urls.py": (
                "from django.urls import path\n"
                "from app.views import View\n"
                "urlpatterns=[path('a', View.as_view())]\n"
            ),
            "app/views.py": "class View:\n    def get(self):\n        pass\n",
            "other/urls.py": (
                "from django.urls import path\n"
                "from other.views import View\n"
                "urlpatterns=[path('b', View.as_view())]\n"
            ),
            "other/views.py": "class View:\n    def get(self):\n        pass\n",
        },
    )
    for endpoint in snap.by_kind(NodeKind.API_ENDPOINT):
        handlers = [
            snap.nodes[e.from_id]
            for e in snap.edges
            if e.kind == EdgeKind.HANDLES and e.to_id == endpoint.id
        ]
        assert {h.service_id for h in handlers} == {endpoint.service_id}
        assert len(handlers) == 1


def test_decorator_wrapper_outbound_is_reachable(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": (
                "from fastapi import FastAPI\n"
                "from app.guard import guard\n"
                "app = FastAPI()\n"
                "@app.get('/items')\n"
                "@guard\n"
                "def listing():\n"
                "    pass\n"
            ),
            "app/guard.py": (
                "import requests\n"
                "def guard(fn):\n"
                "    def wrapped():\n"
                "        requests.get('https://example.invalid/audit')\n"
                "        return fn()\n"
                "    return wrapped\n"
            ),
        },
    )
    endpoint = snap.by_kind(NodeKind.API_ENDPOINT)[0]
    assert [target.name for _, target in snap.outbound_calls(endpoint.id)] == [
        "GET https://example.invalid/audit"
    ]


def test_explicit_client_package_links_inherited_wrapper(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": (
                "from generated_sdk.api import BaseApi\n"
                "class Api(BaseApi):\n"
                "    pass\n"
                "def run():\n"
                "    api = Api()\n"
                "    return api.list_items()\n"
            ),
            "other/main.py": (
                "from fastapi import FastAPI\n"
                "app=FastAPI()\n"
                "@app.get('/items', operation_id='list_items')\n"
                "def listing():\n"
                "    pass\n"
            ),
        },
    )
    config = tmp_path / "axon.yaml"
    config.write_text(
        config.read_text().replace(
            "    roots: [app]\n",
            (
                "    roots: [app]\n"
                "    clients:\n"
                "      - id: sdk\n"
                "        packages: [generated_sdk]\n"
                "        target_service: other\n"
            ),
        )
    )
    snap = index_repo(tmp_path, use_cache=False, workers=1)[0]
    edges = [
        e
        for e in snap.edges
        if e.kind == EdgeKind.CALLS_API and snap.nodes[e.from_id].name == "run"
    ]
    assert len(edges) == 1
    assert snap.nodes[edges[0].to_id].service_id == "other"
    assert edges[0].evidence.rule == "client:sdk"


def test_imported_viewset_custom_action_mount_resolves_handler(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/urls.py": (
                "from django.urls import path, include\n"
                "from rest_framework.routers import DefaultRouter\n"
                "from app.views import ItemViewSet\n"
                "router=DefaultRouter()\n"
                "router.register('items', ItemViewSet)\n"
                "urlpatterns=[path('api/', include(router.urls))]\n"
            ),
            "app/views.py": (
                "from rest_framework.viewsets import ViewSet\n"
                "from rest_framework.decorators import action\n"
                "class ItemViewSet(ViewSet):\n"
                "    def list(self, request):\n"
                "        pass\n"
                "    @action(detail=True, methods=['post'])\n"
                "    def activate(self, request, pk):\n"
                "        pass\n"
            ),
        },
    )
    eps = snap.by_kind(NodeKind.API_ENDPOINT)
    assert {(e.props["method"], e.props["path"]) for e in eps} == {
        ("GET", "/api/items"),
        ("POST", "/api/items/{pk}/activate"),
    }
    assert {snap.handler_of(e.id).file for e in eps} == {"app/views.py"}


def test_index_discovers_owned_swagger_package_without_manual_client(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": (
                "from generated_sdk import Api\n"
                "def run():\n"
                "    api = Api()\n"
                "    return api.list_items()\n"
            ),
            "other/swagger.cfg": "packageName: generated_sdk\n",
            "other/main.py": (
                "from fastapi import FastAPI\n"
                "app=FastAPI()\n"
                "@app.get('/items', operation_id='list_items')\n"
                "def listing():\n"
                "    pass\n"
            ),
        },
    )
    edges = [
        e
        for e in snap.edges
        if e.kind == EdgeKind.CALLS_API and snap.nodes[e.from_id].name == "run"
    ]
    assert len(edges) == 1
    assert snap.nodes[edges[0].to_id].service_id == "other"
    assert edges[0].evidence.rule == "client:discovered-other"


def test_http_wrapper_propagates_literal_url_and_method(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": """from fastapi import FastAPI
from app.transport import send
app = FastAPI()
@app.get('/local')
def local():
    return send('POST', 'https://example.invalid/remote')
""",
            "app/transport.py": """import requests
def send(method, url):
    return requests.request(method, url=url)
""",
            "other/main.py": """from fastapi import FastAPI
app = FastAPI()
@app.post('/remote')
def remote():
    pass
""",
        },
    )
    api = next(ep for ep in snap.by_kind(NodeKind.API_ENDPOINT) if ep.props["path"] == "/local")
    assert any(target.service_id == "other" for _, target in snap.outbound_calls(api.id))


def test_class_field_and_super_calls_keep_shared_library_reachable(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": """from libs.worker import Base, Worker
class Handler(Base):
    def __init__(self):
        self.worker = Worker()
    def run(self):
        self.worker.execute()
        super().audit()
""",
            "libs/worker.py": """class Base:
    def audit(self):
        pass
class Worker:
    def execute(self):
        pass
""",
        },
    )
    assert ("libs/worker.py", "Worker.execute") in _calls(snap, "Handler.run")
    assert ("libs/worker.py", "Base.audit") in _calls(snap, "Handler.run")


def test_spec_base_path_reconciles_code_without_duplicate_endpoint(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/swagger.yaml": """swagger: '2.0'
basePath: /gateway
info: {title: Sample, version: '1'}
paths:
  /items:
    get: {operationId: list_items, responses: {}}
""",
            "app/main.py": """from fastapi import FastAPI
app=FastAPI()
@app.get('/items')
def list_items():
    pass
""",
        },
    )
    endpoints = snap.by_kind(NodeKind.API_ENDPOINT)
    assert len(endpoints) == 1
    assert endpoints[0].props["path"] == "/gateway/items"
    assert endpoints[0].props["source"] == "both"


def test_inferred_factory_constructor_injection_and_thread(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": """from fastapi import FastAPI
from threading import Thread
from libs.worker import Worker
from app.client import api
app=FastAPI()
@app.get('/local')
def local():
    worker = Worker(api)
    Thread(target=worker.run).start()
""",
            "app/client.py": """import generated_sdk
def factory():
    return generated_sdk.V1Api()
api = factory()
""",
            "libs/worker.py": """class Worker:
    def __init__(self, api):
        self.api = api
    def run(self):
        self.api.list_items()
""",
            "other/swagger.cfg": "packageName: generated_sdk\n",
            "other/main.py": """from fastapi import FastAPI
app=FastAPI()
@app.get('/items', operation_id='list_items')
def listing(): pass
""",
        },
    )
    ep = next(e for e in snap.by_kind(NodeKind.API_ENDPOINT) if e.props["path"] == "/local")
    assert {t.service_id for _, t in snap.outbound_calls(ep.id)} == {"other"}


def test_cross_service_path_collision_is_external(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": """import requests
from fastapi import FastAPI
app=FastAPI()
@app.get('/same')
def local():
    requests.get('/same')
""",
            "other/main.py": """from fastapi import FastAPI
app=FastAPI()
@app.get('/same')
def remote(): pass
""",
        },
    )
    edge = next(e for e in snap.edges if e.kind == EdgeKind.CALLS_API)
    assert snap.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API
    assert edge.evidence.rule == "unresolved:path-ambiguous"
    assert len(__import__("json").loads(snap.nodes[edge.to_id].props["candidates"])) == 2


def test_symbolic_url_base_mapping(tmp_path):
    _index(
        tmp_path,
        {
            "app/main.py": """import os, requests
from fastapi import FastAPI
app=FastAPI()
@app.get('/local')
def local():
    url = os.environ.get('REMOTE_URL') + '/items/' + str(42)
    requests.get(url)
""",
            "other/main.py": """from fastapi import FastAPI
app=FastAPI()
@app.get('/items/{id}')
def remote(): pass
""",
        },
    )
    config = tmp_path / "axon.yaml"
    config.write_text(
        config.read_text().replace(
            "  - id: other", "  - id: other\n    base_urls: [env:REMOTE_URL]"
        )
    )
    snap = index_repo(tmp_path, use_cache=False, workers=1)[0]
    edge = next(e for e in snap.edges if e.kind == EdgeKind.CALLS_API)
    assert snap.nodes[edge.to_id].service_id == "other"
    assert edge.evidence.rule == "base-url:env:REMOTE_URL"


def test_dict_constructor_kwargs_resolves_client(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": """import generated_sdk
class Worker:
    def __init__(self, options):
        self.api = options['api']
    def run(self):
        self.api.list_items()
def run():
    worker = Worker({'api': generated_sdk.V1Api()})
    worker.run()
""",
            "other/swagger.cfg": "packageName: generated_sdk\n",
            "other/main.py": """from fastapi import FastAPI
app=FastAPI()
@app.get('/items', operation_id='list_items')
def listing(): pass
""",
        },
    )
    assert any(
        e.kind == EdgeKind.CALLS_API and snap.nodes[e.to_id].service_id == "other"
        for e in snap.edges
    )


def test_drf_implicit_serializer_validation(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": """from django.urls import path
from rest_framework.generics import CreateAPIView
from rest_framework.serializers import Serializer
import requests
class ItemSerializer(Serializer):
    def validate(self, data):
        requests.get('/remote')
        return data
class ItemView(CreateAPIView):
    serializer_class = ItemSerializer
    def perform_create(self, serializer):
        serializer.save()
urlpatterns=[path('local', ItemView.as_view())]
""",
            "other/main.py": """from fastapi import FastAPI
app=FastAPI()
@app.get('/remote')
def remote(): pass
""",
        },
    )
    ep = next(e for e in snap.by_kind(NodeKind.API_ENDPOINT) if e.props["path"] == "/local")
    assert {t.service_id for _, t in snap.outbound_calls(ep.id)} == {"other"}
    assert any(e.evidence.rule == "framework:drf-implicit" for e in snap.edges)


def test_java_client_requires_type_and_suppresses_non_operations(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/Main.java": """import generated_sdk.api.V1Api;
class Main {
 public void run(V1Api api, String generated_sdk) {
  api.list_items();
  api.getId();
  generated_sdk.list_items();
 }
}""",
            "other/swagger.cfg": "apiPackage: generated_sdk.api\n",
            "other/main.py": """from fastapi import FastAPI
app=FastAPI()
@app.get('/items', operation_id='list_items')
def listing(): pass
""",
        },
    )
    edges = [e for e in snap.edges if e.kind == EdgeKind.CALLS_API]
    assert len(edges) == 1
    assert snap.nodes[edges[0].to_id].service_id == "other"
    assert any("suppressed: non-operation" in d for d in snap.diagnostics)


def test_django_model_signal_requires_sender_evidence(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": """from django.urls import path
from django.db.models.signals import post_save
from django.db.models import Model
from django.dispatch import receiver
import requests
class Item(Model): pass
@receiver(post_save, sender=Item)
def changed(sender, instance):
    requests.get('/remote')
def local(request):
    item=Item()
    item.save()
urlpatterns=[path('local', local)]
""",
            "other/main.py": """from fastapi import FastAPI
app=FastAPI()
@app.get('/remote')
def remote(): pass
""",
        },
    )
    ep = next(e for e in snap.by_kind(NodeKind.API_ENDPOINT) if e.props["path"] == "/local")
    assert {t.service_id for _, t in snap.outbound_calls(ep.id)} == {"other"}
    graph = snap.api_graph(ep.id)
    assert any(
        e.get("evidence", {}).get("rule") == "framework:django-signal" for e in graph["edges"]
    )


def test_plain_apiview_does_not_invent_generic_hooks(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": """from rest_framework.views import APIView
from django.urls import path
import requests
class Report(APIView):
    def post(self, request): return 42
    def get_object(self): requests.get('/remote')
urlpatterns=[path('local', Report.as_view())]
""",
        },
    )
    ep = snap.by_kind(NodeKind.API_ENDPOINT)[0]
    assert snap.outbound_calls(ep.id) == []


def test_drf_permission_and_serializer_method_field_scope(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": """from rest_framework.generics import ListAPIView
from rest_framework.serializers import Serializer, SerializerMethodField
from rest_framework.permissions import BasePermission
from django.urls import path
import requests
class Permission(BasePermission):
    def has_permission(self, request, view): requests.get('/permission')
    def has_object_permission(self, request, view, obj): requests.get('/unused-object')
class ItemSerializer(Serializer):
    label=SerializerMethodField()
    def get_label(self, obj): requests.get('/label')
    def get_unused(self, obj): requests.get('/unused-helper')
class ItemView(ListAPIView):
    permission_classes=[Permission]
    serializer_class=ItemSerializer
urlpatterns=[path('local',ItemView.as_view())]
""",
        },
    )
    ep = snap.by_kind(NodeKind.API_ENDPOINT)[0]
    assert {t.props["url"] for _, t in snap.outbound_calls(ep.id)} == {"/permission", "/label"}


def test_imported_url_constant_and_constructor_host(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/config.py": "API_URL = '/items'\n",
            "app/main.py": """from app.config import API_URL
import requests, os
class Worker:
    def __init__(self, host): self.host=host
    def run(self): requests.get(f'{self.host}/items')
def run():
    requests.get(API_URL)
    worker=Worker(os.getenv('REMOTE_URL'))
    worker.run()
""",
            "other/main.py": """from fastapi import FastAPI
app=FastAPI()
@app.get('/items')
def listing(): pass
""",
        },
    )
    urls = {e.props.get("url") for e in snap.edges if e.kind == EdgeKind.CALLS_API}
    assert urls == {"/items", "{env:REMOTE_URL}/items"}


def test_base_class_dispatch_and_dynamic_prefix_are_ambiguous(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/main.py": """class Base:
    def execute(self): pass
class One(Base):
    def execute(self): pass
class Two(Base):
    def execute(self): pass
class Router:
    def run(self, key): getattr(self, f'handle_{key}')()
    def handle_one(self): pass
    def handle_two(self): pass
def run(worker: Base): worker.execute()
"""
        },
    )
    calls = [
        e for e in snap.edges if e.kind == EdgeKind.CALLS and e.evidence.rule == "dispatch:cha"
    ]
    assert len(calls) == 5
    assert all(e.confidence.value == "AMBIGUOUS" for e in calls)


def test_imported_branch_constants_preserve_alternatives(tmp_path):
    snap = _index(
        tmp_path,
        {
            "app/config.py": """if flag:
    BASE='https://a.test'
else:
    BASE='https://b.test'
""",
            "app/main.py": """from app.config import BASE
import requests
def run(): requests.get(BASE+'/items')
""",
        },
    )
    assert {e.props["url"] for e in snap.edges if e.kind == EdgeKind.CALLS_API} == {
        "https://a.test/items",
        "https://b.test/items",
    }
