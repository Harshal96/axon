"""Tests for the pnpm package.json parser."""

from __future__ import annotations

from pathlib import Path

from axon.workers.pnpm.parser import parse_package_json, parse_package_json_string

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_package_json_dep_count() -> None:
    parsed = parse_package_json("fixtures/sample.package.json", str(FIXTURES.parent))
    assert parsed is not None
    assert len(parsed.dependencies) == 5  # 3 deps + 2 devDeps


def test_parse_package_name() -> None:
    parsed = parse_package_json("fixtures/sample.package.json", str(FIXTURES.parent))
    assert parsed is not None
    assert parsed.name == "@company/checkout-ui"
    assert parsed.version == "2.1.0"


def test_parse_workspace_deps_detected() -> None:
    parsed = parse_package_json("fixtures/sample.package.json", str(FIXTURES.parent))
    assert parsed is not None
    internal = [d for d in parsed.dependencies if d.is_internal]
    assert len(internal) == 2
    names = {d.name for d in internal}
    assert "@company/ui-components" in names
    assert "@company/test-utils" in names


def test_parse_external_deps_not_internal() -> None:
    parsed = parse_package_json("fixtures/sample.package.json", str(FIXTURES.parent))
    assert parsed is not None
    external = [d for d in parsed.dependencies if not d.is_internal]
    assert len(external) == 3
    names = {d.name for d in external}
    assert "react" in names
    assert "axios" in names


def test_parse_dev_deps_flagged() -> None:
    parsed = parse_package_json("fixtures/sample.package.json", str(FIXTURES.parent))
    assert parsed is not None
    dev = [d for d in parsed.dependencies if d.is_dev]
    assert len(dev) == 2


def test_parse_nonexistent_returns_none() -> None:
    result = parse_package_json("does/not/exist.json", "/tmp")
    assert result is None


def test_parse_invalid_json_returns_none() -> None:
    result = parse_package_json_string("not json")
    assert result is None


def test_parse_minimal_package() -> None:
    raw = '{"name": "test", "version": "1.0.0"}'
    parsed = parse_package_json_string(raw)
    assert parsed is not None
    assert parsed.name == "test"
    assert len(parsed.dependencies) == 0
