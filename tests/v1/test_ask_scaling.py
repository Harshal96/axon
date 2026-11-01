from axon.v1.ask import ask_deterministic
from axon.v1.models import GraphNode, NodeKind
from tests.evals.ask.fixture import snapshot


def test_warm_service_query_never_scans_symbols(monkeypatch):
    snap = snapshot()
    for number in range(1000):
        snap.add_node(
            GraphNode(id=f"decoy-{number}", kind=NodeKind.SYMBOL, name=f"EnglishWord{number}")
        )
    snap.snapshot_index()
    original = type(snap).by_kind

    def guarded(self, kind):
        assert kind != NodeKind.SYMBOL
        return original(self, kind)

    monkeypatch.setattr(type(snap), "by_kind", guarded)
    response = ask_deterministic(snap, "which apis in scheduler call directory")
    assert response is not None and len(response.rows) == 2
