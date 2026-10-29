"""Graph v2 data model: API-first, build-system agnostic.

Node identity always includes the repo id so the same graph can hold one
monorepo or many small repos side by side.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class NodeKind(StrEnum):
    REPO = "Repo"
    SERVICE = "Service"
    SOURCE_FILE = "SourceFile"
    SYMBOL = "Symbol"
    API_ENDPOINT = "ApiEndpoint"
    API_SPEC = "ApiSpec"
    API_CLIENT = "ApiClient"
    EXTERNAL_API = "ExternalApi"


class EdgeKind(StrEnum):
    CONTAINS = "CONTAINS"  # Repo->Service, Service->SourceFile
    DEFINES = "DEFINES"  # SourceFile->Symbol, ApiSpec->ApiEndpoint
    EXPOSES = "EXPOSES"  # Service->ApiEndpoint
    HANDLES = "HANDLES"  # Symbol->ApiEndpoint
    CALLS = "CALLS"  # Symbol->Symbol
    CALLS_API = "CALLS_API"  # Symbol->ApiEndpoint|ExternalApi
    GENERATED_FROM = "GENERATED_FROM"  # ApiClient->ApiSpec
    IMPORTS = "IMPORTS"  # SourceFile->SourceFile (best effort)


class Confidence(StrEnum):
    EXTRACTED = "EXTRACTED"  # directly parsed
    INFERRED = "INFERRED"  # resolved by a linking rule
    AMBIGUOUS = "AMBIGUOUS"  # multiple candidates / dynamic code
    MANUAL = "MANUAL"  # declared in axon.yaml


class Evidence(BaseModel):
    file: str = ""
    line: int = 0
    rule: str = ""  # detector / linker rule id
    detail: str = ""


class GraphNode(BaseModel):
    id: str
    kind: NodeKind
    name: str = ""
    repo_id: str = ""
    service_id: str = ""
    file: str = ""
    line_start: int = 0
    line_end: int = 0
    props: dict[str, str] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    from_id: str
    to_id: str
    kind: EdgeKind
    confidence: Confidence = Confidence.EXTRACTED
    evidence: Evidence = Field(default_factory=Evidence)
    props: dict[str, str] = Field(default_factory=dict)


def repo_node_id(repo_id: str) -> str:
    return f"repo::{repo_id}"


def service_node_id(repo_id: str, service_id: str) -> str:
    return f"service::{repo_id}::{service_id}"


def file_node_id(repo_id: str, path: str) -> str:
    return f"file::{repo_id}::{path}"


def symbol_node_id(repo_id: str, path: str, qualified: str) -> str:
    return f"symbol::{repo_id}::{path}::{qualified}"


def endpoint_node_id(repo_id: str, service_id: str, method: str, path: str) -> str:
    return f"endpoint::{repo_id}::{service_id}::{method.upper()}:{path}"


def spec_node_id(repo_id: str, path: str) -> str:
    return f"spec::{repo_id}::{path}"


def client_node_id(repo_id: str, client_id: str) -> str:
    return f"client::{repo_id}::{client_id}"


def external_node_id(method: str, template: str) -> str:
    return f"external::{method.upper()}:{template}"
