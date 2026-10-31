from axon.v2.facts import CallFact, FileFacts, SymFact
from axon.v2.indexstate import interface_hash, parse_name_status


def test_interface_ignores_body_and_line_changes():
    a = FileFacts("a.py", "python", symbols=[SymFact("f", "f", "FUNCTION", 1, 2)])
    b = FileFacts(
        "a.py",
        "python",
        symbols=[SymFact("f", "f", "FUNCTION", 2, 30)],
        calls=[CallFact("f", "g", 3)],
    )
    assert interface_hash(a) == interface_hash(b)
    b.returns["f"] = "Client"
    assert interface_hash(a) != interface_hash(b)


def test_name_status_preserves_unusual_paths_and_renames():
    changes = parse_name_status(b"M\0a\nfile.py\0R100\0old name.py\0new name.py\0D\0gone.py\0")
    assert [(c.status, c.path, c.old_path) for c in changes] == [
        ("M", "a\nfile.py", None),
        ("R100", "new name.py", "old name.py"),
        ("D", "gone.py", None),
    ]


def test_roundtrip_and_failed_graph_save_preserve_state(tmp_path):
    import pytest

    from axon.v2.graph import GraphSnapshot
    from axon.v2.indexstate import FileState, IndexState, StateStore

    facts = FileFacts("a.py", "python")
    state = IndexState(
        "repo",
        "old",
        "config",
        "1",
        {
            "a.py": FileState(
                "a.py",
                "hash",
                "key",
                interface_hash(facts),
                facts,
                dependencies={"b.py"},
                lookup_misses={"new"},
            )
        },
        GraphSnapshot(),
    )
    store = StateStore("repo", tmp_path)
    assert store.load() is None
    store.save(state)
    loaded = store.load()
    assert loaded is not None
    assert loaded.files["a.py"].facts == facts
    assert loaded.files["a.py"].dependencies == {"b.py"}
    state.commit = "new"

    def fail(_snapshot):
        raise RuntimeError("graph failed")

    with pytest.raises(RuntimeError):
        store.save_after_graph(state, fail)
    assert store.load().commit == "old"
    store.path.write_bytes(b"corrupt")
    assert store.load() is None
    store.save(state)
    assert store.load().commit == "new"


def test_dirty_readers_and_negative_lookups_are_transitive():
    from axon.v2.indexstate import FileState, expand_dirty_set

    def entry(path, **kwargs):
        return FileState(path, "", "", "", FileFacts(path, "python"), **kwargs)

    files = {
        "a": entry("a", dependencies={"b"}),
        "b": entry("b", lookup_misses={"new"}),
        "c": entry("c", fallback_names={"new"}),
        "d": entry("d"),
    }
    assert expand_dirty_set({"added"}, files, {"new"}) == {"added", "a", "b", "c"}


def test_fallback_threshold_and_metadata():
    from axon.v2.graph import GraphSnapshot
    from axon.v2.indexstate import IndexState, fallback_reason

    state = IndexState("repo", "commit", "config", "1", {}, GraphSnapshot())
    assert fallback_reason(None, "config", "1", 0, 100) == "missing-state"
    assert fallback_reason(state, "changed", "1", 0, 100) == "config-changed"
    assert fallback_reason(state, "config", "2", 0, 100) == "parser-changed"
    assert fallback_reason(state, "config", "1", 15, 100) is None
    assert fallback_reason(state, "config", "1", 16, 100) == "change-threshold"


def test_malformed_diff_rejected():
    import pytest

    for raw in (b"M\0missing", b"R100\0old\0", b"BAD\0path\0"):
        with pytest.raises(ValueError):
            parse_name_status(raw)


def test_resolved_facts_are_separate_from_raw_facts(tmp_path):
    from axon.v2.graph import GraphSnapshot
    from axon.v2.indexstate import FileState, IndexState, StateStore

    raw = FileFacts("a.py", "python")
    resolved = FileFacts("a.py", "python", calls=[CallFact("f", "synthetic", 2)])
    entry = FileState("a.py", "hash", "key", interface_hash(raw), raw, resolved_facts=resolved)
    store = StateStore("repo", tmp_path)
    store.save(IndexState("repo", "commit", "config", "1", {"a.py": entry}, GraphSnapshot()))
    loaded = store.load()
    assert loaded is not None
    assert loaded.files["a.py"].facts.calls == []
    assert loaded.files["a.py"].resolved_facts == resolved


def test_changed_symbol_keys_include_module_and_leaf_names():
    from axon.v2.indexstate import symbols_changed

    facts = FileFacts(
        "pkg/a.py", "python", symbols=[SymFact("Client.send", "send", "METHOD", 1, 2)]
    )
    assert {"pkg.a", "a", "Client.send", "send", "pkg.a.Client.send"} <= symbols_changed(
        None, facts
    )
    assert symbols_changed(facts, facts) == set()


def test_normalized_state_compresses_per_file_without_snapshot(tmp_path):
    import sqlite3

    from axon.v2.graph import GraphSnapshot
    from axon.v2.indexstate import FileState, IndexState, StateStore
    from axon.v2.models import GraphNode, NodeKind, repo_node_id

    snap = GraphSnapshot()
    snap.add_node(
        GraphNode(
            id=repo_node_id("repo"),
            kind=NodeKind.REPO,
            repo_id="repo",
            props={"index_generation": "one"},
        )
    )
    facts = FileFacts("a.py", "python", symbols=[SymFact("f", "f", "FUNCTION", 1, 2)])
    entry = FileState(
        "a.py",
        "hash",
        "key",
        "interface",
        facts,
        node_ids={"symbol"},
        edge_ids={"edge"},
        owner_scope="service:app",
    )
    store = StateStore("repo", tmp_path)
    store.save(IndexState("repo", "commit", "config", "version", {"a.py": entry}, snap))
    with sqlite3.connect(store.path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"meta", "files", "file_nodes", "file_edges"} <= tables
        assert isinstance(db.execute("SELECT facts FROM files").fetchone()[0], bytes)
    loaded = store.load(snap)
    assert loaded is not None
    assert loaded.files["a.py"].node_ids == {"symbol"}
    assert loaded.files["a.py"].owner_scope == "service:app"
    assert loaded.snapshot.nodes == snap.nodes
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE file_edges")
    assert store.load(snap) is None


def test_missing_state_column_is_cache_miss(tmp_path):
    import sqlite3

    from axon.v2.graph import GraphSnapshot
    from axon.v2.indexstate import IndexState, StateStore

    store = StateStore("repo", tmp_path)
    store.save(IndexState("repo", "commit", "config", "1", {}, GraphSnapshot()))
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE meta DROP COLUMN parser_version")
    assert store.load() is None
