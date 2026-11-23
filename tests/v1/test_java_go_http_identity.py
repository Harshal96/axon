"""Outbound transport identity survives parsing and publication to a shared store."""

import json
from pathlib import Path

import pytest

from axon.v1.indexer import index_repo
from axon.v1.models import NodeKind, endpoint_node_id
from axon.v1.store import MemoryStore


def _publish(
    store: MemoryStore, root: Path, repo: str, source: str, *, suffix: str, base: str = ""
) -> None:
    src = root / "src"
    src.mkdir(parents=True)
    (src / f"App.{suffix}").write_text(source)
    language, framework = {
        "py": ("python", "fastapi"),
        "java": ("java", "auto"),
        "go": ("go", "go-http"),
    }[suffix]
    (root / "axon.yaml").write_text(
        json.dumps(
            {
                "version": 1,
                "repo": repo,
                "services": [
                    {
                        "id": "api",
                        "roots": ["src"],
                        "language": language,
                        "framework": framework,
                        "base_urls": [base] if base else [],
                    }
                ],
            }
        )
    )
    snapshot, _ = index_repo(root, workers=1, use_cache=False)
    store.publish_repository(snapshot, repo_id=repo)


def _target(
    store: MemoryStore,
    tmp_path: Path,
    method: str = "get",
    *,
    base: str = "http://ledger:8888",
    repo: str = "ledger",
) -> None:
    _publish(
        store,
        tmp_path / repo,
        repo,
        f"""
from fastapi import FastAPI
app = FastAPI()

@app.{method}("/entries")
def entries():
    return {{}}

@app.get("/entries/{{id}}")
def entry(id: str):
    return {{}}
""",
        suffix="py",
        base=base,
    )


def _outbound(store: MemoryStore):
    return store.load().outbound_endpoints(endpoint_node_id("orders", "api", "GET", "/orders"))


@pytest.mark.parametrize(
    "bases",
    [
        ["http://ledger:8888"],
        ["https://ledger:8888"],
        ["http://ledger:8888", "https://ledger:8888"],
    ],
)
def test_vertx_keeps_host_and_port(tmp_path, bases):
    store = MemoryStore()
    _publish(
        store,
        tmp_path / "orders",
        "orders",
        """
class App {
    private WebClient client;
    public void mount(Router router) {
        router.get("/orders").handler(this::orders);
        router.get("/entries").handler(this::entries);
    }
    public void orders(Object context) {
        client.get(8888, "ledger", "/entries").send();
    }
    public void entries(Object context) {}
}
""",
        suffix="java",
    )
    _target(store, tmp_path, base=bases[0])
    if len(bases) > 1:
        _target(store, tmp_path, base=bases[1], repo="other-scheme")
    _target(store, tmp_path, base="http://ledger:8000", repo="different-port")
    [target] = _outbound(store)
    if len(bases) > 1:
        assert target.kind == NodeKind.EXTERNAL_API
        assert target.props["url"] == "//ledger:8888/entries"
    else:
        assert target.id == endpoint_node_id("ledger", "api", "GET", "/entries")


@pytest.mark.parametrize(
    ("http_method", "remote_method", "link_expected"),
    [
        ("HttpMethod.GET", "get", True),
        ("HttpMethod.GET", "delete", False),
        ("org.springframework.http.HttpMethod.GET", "get", True),
        ("GET", "get", True),
        ("runtimeMethod", "get", False),
    ],
)
def test_resttemplate_exchange_preserves_known_method_and_leaves_unknown_external(
    tmp_path, http_method, remote_method, link_expected
):
    store = MemoryStore()
    _target(store, tmp_path, remote_method)
    _publish(
        store,
        tmp_path / "orders",
        "orders",
        f"""
import static org.springframework.http.HttpMethod.GET;

@RestController
class App {{
    private RestTemplate rest;
    @GetMapping("/orders")
    public Object orders() {{
        return rest.exchange("http://ledger:8888/entries", {http_method}, null, Object.class);
    }}
}}
""",
        suffix="java",
    )
    [target] = _outbound(store)
    if link_expected:
        assert target.id == endpoint_node_id("ledger", "api", "GET", "/entries")
    else:
        assert target.kind == NodeKind.EXTERNAL_API
        assert target.props["method"] == ("UNKNOWN" if http_method == "runtimeMethod" else "GET")


@pytest.mark.parametrize(
    ("call", "expected_path"),
    [
        ('http.Get("http://ledger:8888/entries/" + id)', "/entries/{id}"),
        ('http.Get("http://ledger:8888/" + `entries`)', "/entries"),
        (
            'req, _ := http.NewRequestWithContext(r.Context(), "GET",\n'
            '    "http://ledger:8888/entries", nil)\n'
            "\thttp.DefaultClient.Do(req)",
            "/entries",
        ),
        (
            "req, _ := http.NewRequestWithContext(r.Context(), http.MethodGet,\n"
            '    "http://ledger:8888/entries", nil)\n'
            "\thttp.DefaultClient.Do(req)",
            "/entries",
        ),
        (
            "req, _ := http.NewRequestWithContext(r.Context(), r.Method,\n"
            '    "http://ledger:8888/entries", nil)\n'
            "\thttp.DefaultClient.Do(req)",
            None,
        ),
        (
            'http.Get("http://ledger:8888/entries" + fmt.Sprint(arguments(id, 42)))',
            None,
        ),
    ],
)
def test_go_http_argument_expression_is_not_truncated(tmp_path, call, expected_path):
    store = MemoryStore()
    _publish(
        store,
        tmp_path / "orders",
        "orders",
        f"""
package orders
import "net/http"

func setup() {{
    http.HandleFunc("GET /orders", orders)
}}

func orders(w http.ResponseWriter, r *http.Request) {{
    id := r.PathValue("id")
    {call}
}}
""",
        suffix="go",
    )
    _target(store, tmp_path)
    [target] = _outbound(store)
    if expected_path is None:
        assert target.kind == NodeKind.EXTERNAL_API
    else:
        assert target.id == endpoint_node_id("ledger", "api", "GET", expected_path)
