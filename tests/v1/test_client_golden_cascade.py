"""Client deletion must prove which golden expectations actually lose support."""

import pytest
import yaml

from axon.v1.configedit import ConfigEditor, EditError
from axon.v1.graph import GraphSnapshot
from axon.v1.maintenance import remove_client
from axon.v1.models import EdgeKind, Evidence, GraphEdge, GraphNode, NodeKind


def graph(*rules):
    snap = GraphSnapshot()
    for identity, kind, service, props in [
        ("source", NodeKind.API_ENDPOINT, "app", {"method": "GET", "path": "/run"}),
        ("handler", NodeKind.SYMBOL, "app", {}),
        ("target", NodeKind.API_ENDPOINT, "target", {"method": "GET", "path": "/target"}),
    ]:
        snap.add_node(
            GraphNode(id=identity, kind=kind, repo_id="demo", service_id=service, props=props)
        )
    snap.add_edge(GraphEdge(from_id="handler", to_id="source", kind=EdgeKind.HANDLES))
    for rule in rules:
        snap.add_edge(
            GraphEdge(
                from_id="handler",
                to_id="target",
                kind=EdgeKind.CALLS_API,
                evidence=Evidence(file="app/main.py", line=2, rule=rule),
            )
        )
    return snap


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("def run():\n    pass\n")
    (tmp_path / "axon.yaml").write_text(
        "repo: demo\nservices: [{id: app, roots: [app], clients: [{id: sdk}]}]\n"
    )
    (tmp_path / "axon.golden.yaml").write_text("""version: 1
cases:
- id: target
  api: {service: app, method: GET, path: /run}
  expect: [{service: target, path: /target}]
  forbid: [{service: forbidden}]
""")
    return tmp_path


def mock_indexes(monkeypatch, repo, before, after):
    from axon.v1.servicemap import load_service_map

    def index(root, **kwargs):
        return (before if root == repo else after), load_service_map(root)

    monkeypatch.setattr("axon.v1.indexer.index_repo", index)


def test_graph_client_edges_guard_even_without_import_references(repo, monkeypatch):
    mock_indexes(monkeypatch, repo, graph("client:sdk(source-path)"), graph())
    with pytest.raises(EditError) as caught:
        remove_client(ConfigEditor(repo), "sdk")
    assert caught.value.code == 4
    assert any(row["file"] == "app/main.py" for row in caught.value.candidates)


@pytest.mark.parametrize("after_rules,should_remove", [([], True), (["match:method+path"], False)])
def test_cascade_golden_reverifies_only_client_supported_expectations(
    repo, monkeypatch, after_rules, should_remove
):
    mock_indexes(monkeypatch, repo, graph("client:sdk"), graph(*after_rules))
    editor = ConfigEditor(repo)
    plan = remove_client(editor, "sdk", force=True, cascade_golden=True)
    golden = yaml.safe_load(plan.after[str(repo / "axon.golden.yaml")])
    assert bool(golden["cases"][0]["expect"]) is not should_remove
    assert golden["cases"][0]["forbid"] == [{"service": "forbidden"}]
    assert (repo / "axon.yaml").read_text() == plan.before[str(repo / "axon.yaml")]


def test_cascade_golden_keeps_preexisting_failures_and_other_rules(repo, monkeypatch):
    mock_indexes(monkeypatch, repo, graph("client:other"), graph())
    editor = ConfigEditor(repo)
    plan = remove_client(editor, "sdk", force=True, cascade_golden=True)
    golden = yaml.safe_load(plan.after[str(repo / "axon.golden.yaml")])
    assert golden["cases"][0]["expect"] == [{"service": "target", "path": "/target"}]


def test_cascade_golden_keeps_expectation_with_alternative_preexisting_support(repo, monkeypatch):
    mock_indexes(monkeypatch, repo, graph("client:sdk", "match:method+path"), graph())
    plan = remove_client(ConfigEditor(repo), "sdk", force=True, cascade_golden=True)
    golden = yaml.safe_load(plan.after[str(repo / "axon.golden.yaml")])
    assert golden["cases"][0]["expect"] == [{"service": "target", "path": "/target"}]


def test_cascade_drops_case_left_without_expectations(repo, monkeypatch):
    path = repo / "axon.golden.yaml"
    golden = yaml.safe_load(path.read_text())
    golden["cases"][0].pop("forbid")
    path.write_text(yaml.safe_dump(golden))
    mock_indexes(monkeypatch, repo, graph("client:sdk"), graph())
    plan = remove_client(ConfigEditor(repo), "sdk", force=True, cascade_golden=True)
    assert yaml.safe_load(plan.after[str(path)])["cases"] == []
