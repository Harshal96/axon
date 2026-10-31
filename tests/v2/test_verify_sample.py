from pathlib import Path

import yaml

from axon.v2.graph import GraphSnapshot
from axon.v2.models import Evidence, GraphEdge, GraphNode, NodeKind
from axon.v2.verify_sample import apply_verdicts, export_golden, sample_edges


def fixture_graph(root: Path) -> GraphSnapshot:
    (root / "app.py").write_text(
        "# route\ndef handler():\n    helper()\ndef helper():\n    request()\n"
    )
    snap = GraphSnapshot()
    for node in [
        GraphNode(
            id="api",
            kind=NodeKind.API_ENDPOINT,
            service_id="source",
            props={"method": "GET", "path": "/local"},
            file="app.py",
            line_start=1,
        ),
        GraphNode(
            id="target",
            kind=NodeKind.API_ENDPOINT,
            service_id="target",
            props={"method": "POST", "path": "/remote"},
        ),
        GraphNode(id="handler", kind=NodeKind.SYMBOL, file="app.py", line_start=2, line_end=3),
        GraphNode(id="helper", kind=NodeKind.SYMBOL, file="app.py", line_start=4, line_end=5),
    ]:
        snap.add_node(node)
    snap.add_edge(
        GraphEdge(
            from_id="handler", to_id="api", kind="HANDLES", evidence=Evidence(file="app.py", line=1)
        )
    )
    snap.add_edge(GraphEdge(from_id="handler", to_id="helper", kind="CALLS"))
    snap.add_edge(
        GraphEdge(
            from_id="helper",
            to_id="target",
            kind="CALLS_API",
            evidence=Evidence(file="app.py", line=5, rule="method-path"),
        )
    )
    return snap


def test_sample_is_repeatable_and_has_shortest_source_chain(tmp_path):
    snap = fixture_graph(tmp_path)
    report = sample_edges(snap, tmp_path, services=1, apis=1, seed=4)
    assert report == sample_edges(snap, tmp_path, services=1, apis=1, seed=4)
    row = report["rows"][0]
    assert [node["id"] for node in row["chain"]] == ["handler", "helper"]
    assert row["call_site"]["snippet"] == "    request()"
    assert row["rule"] == "method-path"
    assert row["flags"] == []


def test_sample_flags_bad_ranges_and_exports_only_confirmed(tmp_path):
    snap = fixture_graph(tmp_path)
    snap.edges[-1].evidence.line = 8
    report = sample_edges(snap, tmp_path, seed=1)
    row = report["rows"][0]
    assert "call-site-outside-caller" in row["flags"]
    assert export_golden(report)["cases"] == []
    row["verdict"] = "ok"
    data = export_golden(report)
    assert yaml.safe_load(yaml.safe_dump(data)) == data
    assert data["cases"][0]["expect"][0]["evidence"] == {"file": "app.py", "line": 8}


def test_sample_does_not_read_sources_outside_repo(tmp_path):
    snap = fixture_graph(tmp_path)
    snap.nodes["handler"].file = "../private.py"
    row = sample_edges(snap, tmp_path)["rows"][0]
    assert row["handler"]["snippet"] == ""


def test_verdicts_do_not_survive_source_or_handler_changes(tmp_path):
    snap = fixture_graph(tmp_path)
    reviewed = sample_edges(snap, tmp_path)
    reviewed["rows"][0]["verdict"] = "ok"
    (tmp_path / "app.py").write_text("# changed\ndef handler():\n    changed()\n")
    fresh = sample_edges(snap, tmp_path)
    apply_verdicts(fresh, reviewed)
    assert fresh["rows"][0]["verdict"] == ""
    assert fresh["rows"][0]["id"] != reviewed["rows"][0]["id"]


def test_sample_retains_distinct_call_sites_to_same_target(tmp_path):
    snap = fixture_graph(tmp_path)
    duplicate = snap.edges[-1].model_copy(deep=True)
    duplicate.evidence.line = 4
    snap.add_edge(duplicate)
    rows = sample_edges(snap, tmp_path)["rows"]
    assert {row["call_site"]["line"] for row in rows} == {4, 5}
