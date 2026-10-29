from __future__ import annotations

from pathlib import Path

import pytest

from axon.v2.facts import (
    CallFact,
    FileFacts,
    HttpFact,
    IncludeFact,
    ParseDiagnostic,
    RouteFact,
    SymFact,
    facts_from_dict,
    facts_to_dict,
)
from axon.v2.factscache import cache_key, load_facts, store_facts

pytestmark = pytest.mark.tier1


def _facts() -> FileFacts:
    return FileFacts(
        path="svc/app.py",
        language="python",
        symbols=[SymFact("f", "f", "FUNCTION", 1, 3, "x")],
        calls=[CallFact("f", "g", 2, "1")],
        routes=[RouteFact("GET", "/x", "f", 1, "fastapi", "f")],
        includes=[IncludeFact("api/", "app.urls", 4)],
        http_calls=[HttpFact("f", 2, "GET", "http://a/x", "httpx", "c.m")],
        imports=["os"],
        diagnostics=[ParseDiagnostic(1, "SyntaxWarning", "invalid escape sequence '\\d'")],
        diagnostics_truncated=3,
    )


def test_facts_round_trip_covers_every_field():
    facts = _facts()
    assert facts_from_dict(facts_to_dict(facts)) == facts
    assert facts_from_dict({}) == FileFacts(path="", language="")


def test_cache_store_load_and_key(tmp_path: Path):
    key = cache_key(".py", 1, b"x = 1\n")
    assert load_facts(key, tmp_path) is None
    store_facts(key, _facts(), tmp_path)
    loaded = load_facts(key, tmp_path)
    assert loaded == _facts()
    assert cache_key(".py", 2, b"x = 1\n") != key
    assert cache_key(".py", 1, b"x = 2\n") != key


def test_corrupt_entry_is_a_miss(tmp_path: Path):
    key = cache_key(".py", 1, b"x = 1\n")
    store_facts(key, _facts(), tmp_path)
    entry = next(tmp_path.rglob("*.json"))
    entry.write_text("{not json", encoding="utf-8")
    assert load_facts(key, tmp_path) is None
    assert not entry.exists()
