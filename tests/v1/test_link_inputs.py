from axon.v1.graph import GraphSnapshot
from axon.v1.graph_delta import GraphDelta, replace_repositories
from axon.v1.models import GraphEdge, GraphNode, NodeKind


def test_snapshot_link_metadata_round_trip_and_copy():
    from axon.v1.facts import HttpFact
    from axon.v1.link_inputs import (
        PinnedCallInput,
        PreparedCallInput,
        RepoLinkInputs,
        RepoLinkReport,
    )
    from axon.v1.servicemap import ServiceMap

    inputs = RepoLinkInputs(
        repo_id="a",
        service_map=ServiceMap(repo="a"),
        prepared=[
            PreparedCallInput(
                "caller", "a.py", "service", "svc", 1, "", HttpFact("f", 3, bound={"x": "y"})
            )
        ],
        pinned=[
            PinnedCallInput(GraphEdge(from_id="caller", to_id="remote", kind="CALLS_API"), "remote")
        ],
    )
    snap = GraphSnapshot(
        link_inputs={"a": inputs},
        link_reports={"a": RepoLinkReport(unresolved=["unknown"])},
        publication_id="pub",
    )
    loaded = GraphSnapshot.model_validate_json(snap.model_dump_json())
    assert loaded.model_dump() == snap.model_dump()
    copied = GraphDelta().apply(snap)
    copied.link_inputs["a"].prepared[0].fact.bound["x"] = "changed"
    copied.link_reports["a"].unresolved.append("new")
    assert snap.link_inputs["a"].prepared[0].fact.bound == {"x": "y"}
    assert snap.link_reports["a"].unresolved == ["unknown"]
    assert (
        GraphDelta.between(GraphSnapshot(), snap).apply(GraphSnapshot()).model_dump()
        == snap.model_dump()
    )


def test_replacement_uses_repo_nodes_and_preserves_foreign_callers():
    a = GraphNode(id="a", kind=NodeKind.SYMBOL, repo_id="a")
    b = GraphNode(id="b", kind=NodeKind.API_ENDPOINT, repo_id="b")
    rb = GraphNode(id="repo::b", kind=NodeKind.REPO, repo_id="b")
    edge = GraphEdge(from_id="a", to_id="b", kind="CALLS_API")
    base = GraphSnapshot(nodes={n.id: n for n in (a, b, rb)}, edges=[edge])
    incoming = GraphSnapshot(nodes={n.id: n for n in (b, rb)})
    assert replace_repositories(base, incoming).edges == [edge]
    placeholder = GraphNode(id="external", kind=NodeKind.EXTERNAL_API, repo_id="b")
    assert (
        "b" in replace_repositories(base, GraphSnapshot(nodes={placeholder.id: placeholder})).nodes
    )
    empty = replace_repositories(base, GraphSnapshot(), repo_ids={"b"})
    assert set(empty.nodes) == {"a"}
    assert empty.edges == []


def test_replacement_removes_only_selected_repository_metadata():
    from axon.v1.link_inputs import RepoLinkInputs, RepoLinkReport
    from axon.v1.servicemap import ServiceMap

    base = GraphSnapshot(
        link_inputs={
            r: RepoLinkInputs(repo_id=r, service_map=ServiceMap(repo=r)) for r in ("a", "b")
        },
        link_reports={r: RepoLinkReport(unresolved=[r]) for r in ("a", "b")},
        publication_id="published",
    )
    result = replace_repositories(base, GraphSnapshot(), repo_ids={"b"})
    assert set(result.link_inputs) == {"a"}
    assert set(result.link_reports) == {"a"}
    assert result.publication_id == "published"
    assert set(base.link_inputs) == {"a", "b"}


def test_frozen_snapshot_has_empty_link_inputs():
    from pathlib import Path

    from axon.v1.store import load_snapshot_file

    snap = load_snapshot_file(Path(__file__).parents[1] / "fixtures/compat/snapshot-v1.json")
    assert snap.link_inputs == {}
    assert snap.link_reports == {}
    assert snap.publication_id == ""
