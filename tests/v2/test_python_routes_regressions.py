from axon.v2.facts import facts_from_dict, facts_to_dict
from axon.v2.parse_python import parse_python


def test_safe_calls_and_metadata():
    facts = parse_python(
        "pkg/views.py",
        """from pkg.clients import Client as Remote
class View(BaseView):
    def fetch(self) -> Remote:
        client: Remote = Remote()
        return client
def endpoint():
    factory().delete('/items')
""",
    )
    assert all(call.called != "delete" for call in facts.calls)
    assert facts.import_map["Remote"] == "pkg.clients.Client"
    assert facts.superclasses["View"] == ["BaseView"]
    assert facts.returns["View.fetch"] == "Remote"
    assert facts.bindings[0].var == "client"
    assert facts_from_dict(facts_to_dict(facts)) == facts


def test_wrapped_django_action_map_and_imported_include():
    facts = parse_python(
        "pkg/urls.py",
        """from pkg import routes as child
urlpatterns = [
    path('items/', guarded(ItemView.as_view({'get': 'list', 'post': 'create'}))),
    path('v1/', include(child)),
]
""",
    )
    assert {(r.method, r.handler) for r in facts.routes} == {
        ("GET", "ItemView.list"),
        ("POST", "ItemView.create"),
    }
    assert facts.includes[0].module == "pkg"
    assert facts.includes[0].symbol == "routes"


def test_flask_and_aiohttp_routes():
    facts = parse_python(
        "app.py",
        """from flask import Flask
from aiohttp import web
app = Flask(__name__)
routes = web.RouteTableDef()
@app.route('/items', methods=['GET', 'POST'])
def items(): pass
@routes.get('/status')
async def status(request): pass
app.add_url_rule('/other', view_func=items, methods=['PUT'])
app.router.add_get('/ready', status)
""",
    )
    assert {(r.method, r.path, r.framework) for r in facts.routes} == {
        ("GET", "/items", "flask"),
        ("POST", "/items", "flask"),
        ("GET", "/status", "aiohttp"),
        ("PUT", "/other", "flask"),
        ("GET", "/ready", "aiohttp"),
    }


def test_http_bound_url_and_generated_operation_name():
    facts = parse_python(
        "client.py",
        """import requests
def read_item():
    url = 'https://example.invalid/items'
    requests.get(url)
class ItemsApi:
    def list_items_with_http_info(self):
        return self.api_client.call_api('/items', 'GET')
""",
    )
    assert facts.http_calls[0].url == "https://example.invalid/items"
    assert facts.http_calls[1].call_name == "ItemsApi.list_items_with_http_info"


def test_django_router_actions_and_pattern_composition():
    facts = parse_python(
        "pkg/urls.py",
        """from rest_framework.routers import DefaultRouter
from rest_framework.decorators import action
class ItemView:
    def list(self): pass
    def retrieve(self): pass
    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self): pass
router = DefaultRouter()
router.register('items', ItemView)
base = [path('status/', status)]
urlpatterns = [*base] + [path('api/', include(router.urls))]
urlpatterns += [path('ready/', ready)]
""",
    )
    routes = {(route.method, route.path, route.handler) for route in facts.routes}
    assert ("GET", "/api/items", "ItemView.list") in routes
    assert ("GET", "/api/items/{pk}", "ItemView.retrieve") in routes
    assert ("POST", "/api/items/{pk}/activate", "ItemView.activate") in routes
    assert ("ANY", "/status", "status") in routes
    assert ("ANY", "/ready", "ready") in routes


def test_decorator_calls_belong_to_enclosing_scope_and_nested_functions_are_functions():
    facts = parse_python(
        "views.py",
        """@guard(make_policy())
def endpoint():
    @retry(policy())
    def inner():
        work()
    return inner()
""",
    )
    assert ("", "make_policy") in {(c.caller, c.called) for c in facts.calls}
    assert ("endpoint", "policy") in {(c.caller, c.called) for c in facts.calls}
    assert next(s for s in facts.symbols if s.qualified == "endpoint.inner").kind == "FUNCTION"


def test_named_django_patterns_only_emit_at_mounts():
    facts = parse_python(
        "pkg/urls.py",
        """base = [path('items/', items)]
extra = [*base] + [path('status/', status)]
extra += [path('ready/', ready)]
urlpatterns = [path('api/', include((extra, 'catalog')))]
""",
    )
    assert {(r.path, r.handler) for r in facts.routes} == {
        ("/api/items", "items"),
        ("/api/status", "status"),
        ("/api/ready", "ready"),
    }


def test_imported_named_pattern_list_is_explicit_metadata():
    facts = parse_python(
        "pkg/urls.py",
        """from .children import public_patterns as public
urlpatterns = [path('api/', include(public))]
""",
    )
    assert facts.includes[0].module == "pkg.children"
    assert facts.includes[0].symbol == "public_patterns"
    child = parse_python("pkg/children.py", "public_patterns = [path('items/', items)]")
    assert child.routes == []
    assert child.pattern_routes["public_patterns"][0].path == "/items"
    assert facts_from_dict(facts_to_dict(child)) == child


def test_http_request_uses_bound_url_and_keyword_method():
    facts = parse_python(
        "views.py",
        """import requests
def fetch():
    url = 'https://example.invalid/items'
    requests.request('POST', url)
    requests.request(method='PUT', url=url)
""",
    )
    assert [(h.method, h.url) for h in facts.http_calls] == [
        ("POST", "https://example.invalid/items"),
        ("PUT", "https://example.invalid/items"),
    ]


def test_decorator_application_and_returned_closure_are_reachable():
    facts = parse_python(
        "views.py",
        """def guard(fn):
    def wrapped():
        audit()
        return fn()
    return wrapped
@guard
def endpoint(): pass
""",
    )
    calls = {(c.caller, c.called) for c in facts.calls}
    assert ("endpoint", "guard") in calls
    assert ("guard", "guard.wrapped") in calls


def test_django_regex_named_group_normalization_balances_parentheses():
    from axon.v2.facts import normalize_path

    assert normalize_path(r"^items/(?P<key>(?:[a-z]+|[0-9]+))/$") == "/items/{key}"


def test_imported_viewset_registration_and_action_metadata():
    facts = parse_python(
        "pkg/routes.py",
        """from rest_framework.routers import DefaultRouter
from .views import ItemView
router = DefaultRouter()
router.register('items', ItemView)
urlpatterns = [path('v1/', include(router.urls))]
""",
    )
    registration = facts.router_registrations[0]
    assert (registration.router, registration.prefix, registration.viewset) == (
        "router",
        "items",
        "ItemView",
    )
    assert facts.includes[0].kind == "drf"
    child = parse_python(
        "pkg/views.py",
        """from rest_framework.decorators import action
class ItemView:
    def list(self): pass
    @action(detail=True, methods=['POST'], url_path='activate')
    def activate(self): pass
""",
    )
    assert {(r.method, r.path, r.handler) for r in child.view_actions["ItemView"]} == {
        ("GET", "/", "ItemView.list"),
        ("POST", "/{pk}/activate", "ItemView.activate"),
    }
    assert facts_from_dict(facts_to_dict(facts)) == facts
    assert facts_from_dict(facts_to_dict(child)) == child


def test_django_module_qualified_handlers_retain_import_evidence():
    facts = parse_python(
        "pkg/urls.py",
        """from . import views
urlpatterns = [path('items/', views.items), path('detail/', views.ItemView.as_view())]
""",
    )
    assert {route.handler for route in facts.routes} == {"views.items", "views.ItemView"}


def test_static_route_strings_and_factory_helpers():
    facts = parse_python(
        "app/routes.py",
        """from fastapi import APIRouter
PREFIX = '/api'
router = APIRouter(prefix=PREFIX + '/v1')
ITEMS = 'items'
@router.get('/{name}'.format(name=ITEMS))
def listing():
    client = get_internal_api(swagger_cli=generated_sdk)
    return client.list_items()
""",
    )
    assert [(r.method, r.path) for r in facts.routes] == [("GET", "/api/v1/items")]
    assert next(b for b in facts.bindings if b.var == "client").source == "generated_sdk"


def test_direct_constructor_receiver_keeps_domain_call_without_primitive_tail():
    facts = parse_python(
        "app/main.py",
        """def run():
    Worker().execute()
    text.strip().split(',')
    'a,b'.split(',')
""",
    )
    calls = {c.called for c in facts.calls}
    assert "Worker.execute" in calls
    assert "split" not in calls
