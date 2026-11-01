from axon.v1.fingerprints import INDEXER_FINGERPRINT, PARSER_FINGERPRINTS, module_fingerprint


def test_ast_fingerprint_ignores_formatting_and_comments():
    assert module_fingerprint("x = 1 # comment\n") == module_fingerprint("x=1\n")
    assert module_fingerprint("x=1") != module_fingerprint("x=2")


def test_ast_fingerprint_preserves_semantic_fields():
    assert module_fingerprint("def f(): pass") != module_fingerprint("def f(x): pass")
    assert module_fingerprint("x = []") != module_fingerprint("x = ()")
    assert module_fingerprint("x = None") != module_fingerprint("x = ...")
    assert module_fingerprint("x = True") != module_fingerprint("x = 1")


def test_build_fingerprints_are_content_addressed():
    assert set(PARSER_FINGERPRINTS) == {".py", ".java", ".go"}
    assert all(len(value) == 64 for value in PARSER_FINGERPRINTS.values())
    assert len(INDEXER_FINGERPRINT) == 64


def test_indexer_upgrade_forces_full_resolution():
    import json

    from axon.v1.graph import GraphSnapshot
    from axon.v1.indexstate import IndexState, fallback_reason

    previous = IndexState(
        "repo", "abc", "config", json.dumps({"indexer": "old", "parsers": {}}), {}, GraphSnapshot()
    )
    assert (
        fallback_reason(previous, "config", json.dumps({"indexer": "new", "parsers": {}}), 0, 100)
        == "indexer-changed"
    )


def test_fingerprint_golden_requires_explicit_review():
    import json
    from pathlib import Path

    from axon.v1.fingerprints import STATE_FINGERPRINT

    expected = json.loads(Path(__file__).with_name("build_fingerprints.json").read_text())
    assert expected == {
        "indexer": INDEXER_FINGERPRINT,
        "parsers": PARSER_FINGERPRINTS,
        "state": STATE_FINGERPRINT,
    }
