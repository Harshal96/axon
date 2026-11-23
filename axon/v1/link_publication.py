"""Generation visibility and versioned, bounded private link metadata.

Each typed per-repository value is JSON encoded, then base64 split into at most
8,192-character ASCII payloads. Every row repeats its identity, chunk count and
SHA-256 digest. A value is capped at 64 MiB of JSON (no decompression involved).
Owners and generations are opaque producer tags: callers select published rows
before decoding; this codec never infers or changes publication ownership.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic_core import PydanticSerializationError

from axon.v1.link_inputs import RepoLinkInputs, RepoLinkReport
from axon.v1.models import GraphSchemaError

FORMAT_VERSION = 1
MAX_PAYLOAD_CHARS = 8192
MAX_RECORD_BYTES = 64 * 1024 * 1024
_MAX_ENCODED_CHARS = 4 * ((MAX_RECORD_BYTES + 2) // 3)
_MAX_CHUNKS = (_MAX_ENCODED_CHARS + MAX_PAYLOAD_CHARS - 1) // MAX_PAYLOAD_CHARS


def _active_link(variable: str) -> str:
    return (
        f"(({variable}._axon_link_generation IS NULL AND NOT EXISTS {{ "
        "MATCH (_axon_link_pub:_AxonLinkPublication {id: 'global'}) }) "
        "OR EXISTS { MATCH (_axon_link_pub:_AxonLinkPublication {id: 'global'}) "
        f"WHERE _axon_link_pub.generation = {variable}._axon_link_generation }})"
    )


def active_node_predicate(variable: str) -> str:
    """Public node predicate, including globally versioned ExternalApi nodes."""
    structural = (
        f"(({variable}._axon_generation IS NULL AND NOT EXISTS {{ "
        f"MATCH (_axon_pub:_AxonPublication) WHERE _axon_pub.repo_id = {variable}.repo_id }}) "
        f"OR EXISTS {{ MATCH (_axon_pub:_AxonPublication) "
        f"WHERE _axon_pub.repo_id = {variable}._axon_owner "
        f"AND _axon_pub.generation = {variable}._axon_generation }})"
    )
    return (
        f"NONE(_axon_label IN labels({variable}) WHERE _axon_label STARTS WITH '_Axon') "
        f"AND ((NOT {variable}:ExternalApi AND {structural}) "
        f"OR ({variable}:ExternalApi AND {_active_link(variable)}))"
    )


def active_relationship_predicate(variable: str) -> str:
    """Only CALLS_API uses the global link marker; endpoints scope other kinds."""
    return f"(type({variable}) <> 'CALLS_API' OR {_active_link(variable)})"


def public_properties(properties: Mapping[str, str]) -> dict[str, str]:
    """Remove private storage properties, including future metadata fields."""
    return {key: value for key, value in properties.items() if not key.startswith("_axon_")}


def public_query_value(value: Any) -> Any:
    """Strip private keys recursively before a driver value can be stringified.

    Neo4j Node and Relationship implement Mapping. Paths also contain endpoint
    nodes, so flatten those explicitly instead of retaining a revealing repr.
    """
    if isinstance(value, Mapping):
        return {
            key: public_query_value(item)
            for key, item in value.items()
            if not isinstance(key, str) or not key.startswith("_axon_")
        }
    if isinstance(value, (list, tuple)):
        return [public_query_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # The driver is optional for in-memory/file users of this module.
    from neo4j.graph import Path

    if isinstance(value, Path):
        return {
            "nodes": public_query_value(value.nodes),
            "relationships": public_query_value(value.relationships),
        }
    return value


class _MetadataRow(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    format_version: int
    owner: str
    generation: str
    kind: Literal["inputs", "report"]
    key: str
    chunk_index: int = Field(ge=0, lt=_MAX_CHUNKS)
    chunk_count: int = Field(ge=1, le=_MAX_CHUNKS)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: str = Field(min_length=1, max_length=MAX_PAYLOAD_CHARS)


_INPUT_ADAPTER = TypeAdapter(RepoLinkInputs)
_REPORT_ADAPTER = TypeAdapter(RepoLinkReport)
_Value = TypeVar("_Value", RepoLinkInputs, RepoLinkReport)


def _error(detail: str) -> GraphSchemaError:
    return GraphSchemaError(f"Axon link metadata {detail}; upgrade Axon or re-index to rebuild it.")


def _check_input(value: RepoLinkInputs, key: str) -> None:
    if type(value.format_version) is not int or value.format_version != FORMAT_VERSION:
        raise _error(f"uses unsupported input format {value.format_version!r}")
    if value.repo_id != key:
        raise _error(f"repo key {key!r} does not match input repo {value.repo_id!r}")


def _encode(
    values: Mapping[str, _Value],
    adapter: TypeAdapter[_Value],
    kind: Literal["inputs", "report"],
    *,
    owner: str,
    generation: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, value in sorted(values.items()):
        if isinstance(value, RepoLinkInputs):
            _check_input(value, key)
        try:
            raw = adapter.dump_json(value, warnings="error")
        except PydanticSerializationError as exc:
            raise _error(f"has invalid typed data for {key!r}") from exc
        if len(raw) > MAX_RECORD_BYTES:
            raise _error(f"record {key!r} exceeds {MAX_RECORD_BYTES} bytes")
        encoded = base64.b64encode(raw).decode("ascii")
        count = (len(encoded) + MAX_PAYLOAD_CHARS - 1) // MAX_PAYLOAD_CHARS
        digest = hashlib.sha256(raw).hexdigest()
        for index in range(count):
            try:
                row = _MetadataRow(
                    format_version=FORMAT_VERSION,
                    owner=owner,
                    generation=generation,
                    kind=kind,
                    key=key,
                    chunk_index=index,
                    chunk_count=count,
                    sha256=digest,
                    payload=encoded[index * MAX_PAYLOAD_CHARS : (index + 1) * MAX_PAYLOAD_CHARS],
                )
            except ValidationError as exc:
                raise _error(f"has invalid record tags for {key!r}") from exc
            rows.append(row.model_dump())
    return rows


def _decode(
    rows: Iterable[Mapping[str, Any]],
    adapter: TypeAdapter[_Value],
    kind: Literal["inputs", "report"],
) -> dict[str, _Value]:
    groups: dict[str, dict[int, _MetadataRow]] = {}
    for raw_row in rows:
        try:
            row = _MetadataRow.model_validate(raw_row)
        except ValidationError as exc:
            raise _error("has an invalid or corrupt row") from exc
        if row.format_version != FORMAT_VERSION:
            raise _error(f"uses unsupported format {row.format_version!r}")
        if row.kind != kind:
            raise _error(f"expected {kind!r} records, found {row.kind!r}")
        chunks = groups.setdefault(row.key, {})
        if chunks:
            first = next(iter(chunks.values()))
            if (row.owner, row.generation, row.chunk_count, row.sha256) != (
                first.owner,
                first.generation,
                first.chunk_count,
                first.sha256,
            ):
                raise _error(f"has conflicting identity/count/digest for {row.key!r}")
        if row.chunk_index in chunks or row.chunk_index >= row.chunk_count:
            raise _error(f"has duplicate or invalid chunk index for {row.key!r}")
        chunks[row.chunk_index] = row
    result: dict[str, _Value] = {}
    for key, chunks in groups.items():
        first = next(iter(chunks.values()))
        if len(chunks) != first.chunk_count:
            raise _error(f"is incomplete for {key!r}")
        encoded = "".join(chunks[index].payload for index in range(first.chunk_count))
        if len(encoded) > _MAX_ENCODED_CHARS:
            raise _error(f"record {key!r} exceeds {MAX_RECORD_BYTES} bytes")
        try:
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > MAX_RECORD_BYTES or hashlib.sha256(raw).hexdigest() != first.sha256:
                raise _error(f"has a corrupt payload for {key!r}")
            value = adapter.validate_json(raw, strict=True)
        except (ValueError, binascii.Error, ValidationError) as exc:
            raise _error(f"has a corrupt payload for {key!r}") from exc
        if isinstance(value, RepoLinkInputs):
            _check_input(value, key)
        result[key] = value
    return result


def encode_link_inputs(
    inputs: Mapping[str, RepoLinkInputs], *, owner: str, generation: str
) -> list[dict[str, Any]]:
    return _encode(inputs, _INPUT_ADAPTER, "inputs", owner=owner, generation=generation)


def decode_link_inputs(rows: Iterable[Mapping[str, Any]]) -> dict[str, RepoLinkInputs]:
    return _decode(rows, _INPUT_ADAPTER, "inputs")


def encode_link_reports(
    reports: Mapping[str, RepoLinkReport], *, owner: str, generation: str
) -> list[dict[str, Any]]:
    return _encode(reports, _REPORT_ADAPTER, "report", owner=owner, generation=generation)


def decode_link_reports(rows: Iterable[Mapping[str, Any]]) -> dict[str, RepoLinkReport]:
    return _decode(rows, _REPORT_ADAPTER, "report")
