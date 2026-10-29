"""Tests for the Swagger/OpenAPI parser."""

from __future__ import annotations

from pathlib import Path

from axon.workers.swagger.parser import parse_swagger_dict, parse_swagger_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_swagger_file_endpoint_count() -> None:
    parsed = parse_swagger_file("fixtures/sample.swagger.json", str(FIXTURES.parent))
    assert parsed is not None
    assert len(parsed.endpoints) == 3


def test_parse_swagger_openapi_version() -> None:
    parsed = parse_swagger_file("fixtures/sample.swagger.json", str(FIXTURES.parent))
    assert parsed is not None
    assert parsed.openapi_version == "3.0.1"


def test_parse_swagger_content_hash() -> None:
    parsed = parse_swagger_file("fixtures/sample.swagger.json", str(FIXTURES.parent))
    assert parsed is not None
    assert len(parsed.content_hash) == 64


def test_parse_swagger_operation_ids() -> None:
    parsed = parse_swagger_file("fixtures/sample.swagger.json", str(FIXTURES.parent))
    assert parsed is not None
    op_ids = {ep.operation_id for ep in parsed.endpoints}
    assert "createCharge" in op_ids
    assert "listCharges" in op_ids
    assert "getCharge" in op_ids


def test_parse_swagger_deprecated_endpoint() -> None:
    parsed = parse_swagger_file("fixtures/sample.swagger.json", str(FIXTURES.parent))
    assert parsed is not None
    deprecated = [ep for ep in parsed.endpoints if ep.deprecated]
    assert len(deprecated) == 1
    assert deprecated[0].operation_id == "getCharge"


def test_parse_swagger_methods() -> None:
    parsed = parse_swagger_file("fixtures/sample.swagger.json", str(FIXTURES.parent))
    assert parsed is not None
    methods = {ep.method for ep in parsed.endpoints}
    assert "POST" in methods
    assert "GET" in methods


def test_parse_swagger_schema_hashes_unique() -> None:
    parsed = parse_swagger_file("fixtures/sample.swagger.json", str(FIXTURES.parent))
    assert parsed is not None
    hashes = [ep.schema_hash for ep in parsed.endpoints]
    # Each endpoint should have a unique schema hash
    assert len(set(hashes)) == len(hashes)


def test_parse_swagger_v2_dict() -> None:
    spec = {
        "swagger": "2.0",
        "info": {"title": "Test", "version": "1.0"},
        "paths": {
            "/api/items": {
                "get": {"operationId": "listItems", "responses": {"200": {"description": "OK"}}}
            }
        },
    }
    parsed = parse_swagger_dict(spec, "test/swagger.json", "abc123")
    assert parsed.openapi_version == "2.0"
    assert len(parsed.endpoints) == 1
    assert parsed.endpoints[0].method == "GET"


def test_parse_swagger_nonexistent_returns_none() -> None:
    result = parse_swagger_file("does/not/exist.json", "/tmp")
    assert result is None


def test_parse_swagger_empty_paths() -> None:
    spec = {"openapi": "3.0.0", "info": {"title": "Empty"}, "paths": {}}
    parsed = parse_swagger_dict(spec, "empty.json", "hash")
    assert len(parsed.endpoints) == 0
