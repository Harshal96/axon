from __future__ import annotations

from axon.db.init import BTREE_INDEXES, CONSTRAINTS, FULLTEXT_INDEXES, verify_schema


def test_all_constraints_created() -> None:
    schema = verify_schema()
    existing = set(schema["constraints"])
    for c in CONSTRAINTS:
        assert c.name in existing, f"Missing constraint: {c.name}"


def test_all_btree_indexes_created() -> None:
    schema = verify_schema()
    existing = set(schema["indexes"])
    for idx in BTREE_INDEXES:
        assert idx.name in existing, f"Missing index: {idx.name}"


def test_all_fulltext_indexes_created() -> None:
    schema = verify_schema()
    existing = set(schema["indexes"])
    for idx in FULLTEXT_INDEXES:
        assert idx.name in existing, f"Missing fulltext index: {idx.name}"


def test_init_is_idempotent() -> None:
    """Running initialize_schema twice must not raise."""
    from axon.db.init import initialize_schema

    initialize_schema()
    initialize_schema()  # second call: all IF NOT EXISTS, should be a no-op
