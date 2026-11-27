"""Source-backed inputs shared by GraphQL frontends and graph assembly."""

import json
from dataclasses import dataclass, field
from typing import Any

from axon.v1.models import GraphEdge


def graphql_dependency_paths(edge: GraphEdge, *, client_only: bool = False) -> set[str]:
    """Repository-relative source inputs recorded when this edge was assembled."""
    encoded = (
        edge.props.get("graphql_client_dependencies") if client_only else None
    ) or edge.props.get("graphql_dependencies")
    if not encoded:
        return set()
    try:
        paths = json.loads(encoded)
    except ValueError:
        return set()
    return {p for p in paths if isinstance(p, str) and p} if isinstance(paths, list) else set()


@dataclass
class DocumentSource:
    path: str
    text: str
    line: int = 1
    column: int = 1
    definitions: list[dict[str, Any]] | None = None


@dataclass
class ResolverBinding:
    coordinate: str
    symbol_id: str
    file: str
    line: int


@dataclass
class ServerInput:
    service_id: str
    file: str
    line: int
    documents: list[DocumentSource]
    resolvers: list[ResolverBinding]
    mounts: list[str]
    dependencies: list[str]
    schema_complete: bool = True


@dataclass
class InvocationInput:
    caller_id: str
    file: str
    line: int
    column: int
    kind: str
    uri: str
    documents: list[DocumentSource]
    dependencies: list[str] = field(default_factory=list)
    operation_name: str = ""
    cache_only: bool = False
