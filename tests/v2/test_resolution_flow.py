"""Contracts for bounded source-level call and URL flow."""

import json

from axon.v2.facts import facts_from_dict, facts_to_dict
from axon.v2.indexer import _CallResolver
from axon.v2.parse_python import parse_python


def test_reference_facts_round_trip():
    facts = parse_python(
        "app.py",
        """
class Runner:
    def __init__(self, api, **kwargs):
        self.api = api
        self.other = kwargs.get('other')
def factory():
    api = sdk.V1Api()
    return api
Runner(factory(), other=sdk.V2Api())
""",
    )
    assert any(b.source == "param:api@Runner.__init__" for b in facts.bindings)
    assert any(b.source == "param:kwargs@Runner.__init__[other]" for b in facts.bindings)
    assert facts.returns_inferred["factory"] == ["api"]
    call = next(c for c in facts.calls if c.called == "Runner")
    assert dict(call.arg_refs) == {0: "call:factory", "other": "call:sdk.V2Api"}
    assert facts_from_dict(json.loads(json.dumps(facts_to_dict(facts)))) == facts


def test_cycles_are_bounded():
    facts = parse_python(
        "app.py",
        """
def a(): return b()
def b(): return a()
x = a()
""",
    )
    resolver = _CallResolver([facts], {})
    assert len(resolver.receiver_types("x", "app.py", "")) < 10
    assert resolver.receiver_types("x", "app.py", "") == resolver.receiver_types("x", "app.py", "")


def test_symbolic_url_shapes():
    facts = parse_python(
        "app.py",
        """
import os, requests
from urllib.parse import urljoin
def run(key):
    requests.get(os.environ['REMOTE_URL'] + '/items/' + str(key))
    requests.get(f"{os.getenv('REMOTE_URL')}/items/{key}")
    requests.get(urljoin(os.getenv('REMOTE_URL'), '/items/{}'.format(key)))
""",
    )
    assert [h.url for h in facts.http_calls] == ["{env:REMOTE_URL}/items/{var}"] * 3


def test_session_and_httpx_base_urls():
    facts = parse_python(
        "app.py",
        """
import requests, httpx, os
def run():
    session = requests.Session()
    session.get(url=os.getenv('REMOTE_URL') + '/items')
    client = httpx.Client(base_url=os.getenv('REMOTE_URL'))
    client.post('/items')
    with requests.Session() as other:
        other.get('/items')
""",
    )
    assert [(h.method, h.url) for h in facts.http_calls] == [
        ("GET", "{env:REMOTE_URL}/items"),
        ("POST", "{env:REMOTE_URL}/items"),
        ("GET", "/items"),
    ]


def test_conditional_url_assignments_keep_both_templates():
    facts = parse_python(
        "app.py",
        """
import requests
def run(flag):
    if flag:
        url = '/one'
    else:
        url = '/two'
    requests.get(url)
""",
    )
    assert {h.url for h in facts.http_calls} == {"/one", "/two"}


def test_dataclass_and_property_client_types():
    facts = parse_python(
        "app.py",
        """
from dataclasses import dataclass
import sdk
@dataclass
class Runner:
    api: object
    @property
    def client(self):
        return self.api
runner = Runner(sdk.V1Api())
""",
    )
    resolver = _CallResolver([facts], {})
    assert "sdk.V1Api" in resolver.receiver_types("self.client", "app.py", "Runner.run")


def test_factory_class_dictionary_dispatch():
    facts = parse_python(
        "app.py",
        """
class One:
    def run(self): pass
class Two:
    def run(self): pass
def factory(kind):
    choices = {1: One, 2: Two}
    return choices[kind]()
""",
    )
    resolver = _CallResolver([facts], {})
    assert {"app.One", "app.Two"} <= set(resolver.receiver_types("factory", "app.py", ""))


def test_inherited_attribute_and_expanded_kwargs():
    facts = parse_python(
        "app.py",
        """
import sdk
class Base:
    def __init__(self, **kwargs): self.api=kwargs.get('api')
class Child(Base):
    def run(self): self.api.list_items()
options={'api': sdk.V1Api()}
child=Child(**options)
""",
    )
    resolver = _CallResolver([facts], {})
    assert "sdk.V1Api" in resolver.receiver_types("self.api", "app.py", "Child.run")


def test_context_manager_without_binding_preserves_http_call():
    facts = parse_python(
        "app.py",
        """import requests
def run():
    with requests.get('https://example.test/items'):
        pass
""",
    )
    assert [h.url for h in facts.http_calls] == ["https://example.test/items"]


def test_literal_urljoin_preserves_root_and_absolute_replacement():
    facts = parse_python(
        "app.py",
        """import requests
from urllib.parse import urljoin
def run():
    requests.get(urljoin('https://example.test/v1/', '/items'))
    requests.get(urljoin('https://example.test/v1/', 'next'))
    requests.get(urljoin('https://example.test/v1/', 'https://other.test/items'))
""",
    )
    assert [h.url for h in facts.http_calls] == [
        "https://example.test/items",
        "https://example.test/v1/next",
        "https://other.test/items",
    ]


def test_instance_request_method_and_url_keywords():
    facts = parse_python(
        "app.py",
        """import httpx, os
def run(method):
    client=httpx.Client(base_url=os.getenv('REMOTE_URL'))
    client.request(method='POST', url='/items')
    client.request(method, '/items')
""",
    )
    assert [(h.method, h.url) for h in facts.http_calls] == [
        ("POST", "{env:REMOTE_URL}/items"),
        ("", "{env:REMOTE_URL}/items"),
    ]


def test_cache_attribute_key_is_not_an_http_url():
    facts = parse_python(
        "app.py",
        """class Cache:
    def find(self):
        return self.cache.get(self.key)
""",
    )
    assert facts.http_calls == []


def test_client_identity_is_scoped_by_owner_service():
    from axon.v2.facts import HttpFact
    from axon.v2.graph import GraphSnapshot
    from axon.v2.linker import EndpointIndex, resolve_http
    from axon.v2.models import GraphNode, NodeKind, endpoint_node_id
    from axon.v2.servicemap import ServiceMap

    sm = ServiceMap.model_validate(
        {
            "services": [
                {
                    "id": "one",
                    "clients": [{"id": "api", "packages": ["alpha"], "target_service": "one"}],
                },
                {
                    "id": "two",
                    "clients": [{"id": "api", "packages": ["beta"], "target_service": "two"}],
                },
            ]
        }
    )
    snap = GraphSnapshot()
    for service in ("one", "two"):
        snap.add_node(
            GraphNode(
                id=endpoint_node_id("sample", service, "GET", "/items"),
                kind=NodeKind.API_ENDPOINT,
                name="fetch",
                repo_id="sample",
                service_id=service,
                props={"method": "GET", "path": "/items", "operation_id": "fetch"},
            )
        )
    fact = HttpFact("run", 1, call_name="beta.fetch", client_id="api", client_owner="two")
    resolved = resolve_http(fact, EndpointIndex(snap), sm, "sample")
    assert resolved is not None
    assert snap.nodes[resolved[0]].service_id == "two"
