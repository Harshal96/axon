"""axon.yaml: the flexible per-repo service map.

Services may live anywhere in a monorepo. Ownership is declared with root
paths/globs, never derived from a build graph. Unknown keys are preserved
on round-trip via `extra` so teams can extend the file freely.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClientDecl(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    # Module/package prefixes whose calls belong to this client, e.g.
    # ["payment_client", "clients.payment"] or Java packages.
    packages: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    # Dotted factory functions whose return value is this client's API
    # object, e.g. ["settings.GET_PAYMENTS_API"]. Lets the indexer bind
    # `api = settings.GET_PAYMENTS_API()` receivers to this client.
    factories: list[str] = Field(default_factory=list)
    # Where this client points: a service id in any imported repo, and/or
    # the spec file it was generated from (repo-relative or repo::path).
    target_service: str = ""
    target_repo: str = ""  # empty = same repo
    spec: str = ""
    base_urls: list[str] = Field(default_factory=list)


class ManualLink(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Match outbound calls by any of these, then point them at an endpoint.
    call_name: str = ""  # e.g. "PaymentClient.charge"
    url_contains: str = ""
    to_service: str
    to_method: str = "GET"
    to_path: str
    to_repo: str = ""


class ServiceDecl(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str = ""
    roots: list[str] = Field(default_factory=list)  # dirs or globs, repo-relative
    language: str = ""  # python | java | go | typescript | mixed
    framework: str = ""  # fastapi | django | spring | vertx | go-http | auto
    specs: list[str] = Field(default_factory=list)
    base_urls: list[str] = Field(default_factory=list)
    clients: list[ClientDecl] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not v or any(c in v for c in ":/ "):
            raise ValueError(f"invalid service id: {v!r}")
        return v


class IndexOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    include_unowned: bool = True  # parse files outside every service/shared root
    exclude_tests: bool = True  # test code never becomes graph nodes by default
    path_suffix_fallback: bool = False  # opt-in unknown gateway prefix matching

    def fingerprint(self) -> str:
        """Stamped on the repo node; a change re-indexes instead of dedup-skipping."""
        return (
            f"include_unowned={int(self.include_unowned)},exclude_tests={int(self.exclude_tests)},"
            f"path_suffix_fallback={int(self.path_suffix_fallback)}"
        )


class ServiceMap(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int = 1
    repo: str = ""  # stable repo id; defaults to directory name
    services: list[ServiceDecl] = Field(default_factory=list)
    shared: list[str] = Field(default_factory=list)  # library roots/globs
    ignore: list[str] = Field(default_factory=list)
    links: list[ManualLink] = Field(default_factory=list)
    index: IndexOptions = Field(default_factory=IndexOptions)

    @field_validator("services")
    @classmethod
    def _unique_ids(cls, v: list[ServiceDecl]) -> list[ServiceDecl]:
        seen: set[str] = set()
        for s in v:
            if s.id in seen:
                raise ValueError(f"duplicate service id: {s.id}")
            seen.add(s.id)
        return v


DEFAULT_IGNORE = [
    "**/.git/**",
    ".worktrees/**",
    "**/.worktrees/**",
    "**/.venv/**",
    "**/venv/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/target/**",
    "**/build/**",
    "**/dist/**",
    "**/.tox/**",
    "**/vendor/**",
]


def _merge(base: Any, patch: Any, key: str = "") -> Any:
    if isinstance(patch, dict) and set(patch) == {"+append"}:
        if not isinstance(base, list) or not isinstance(patch["+append"], list):
            raise ValueError("+append requires a list")
        return [*base, *patch["+append"]]
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for name, value in patch.items():
            out[name] = _merge(
                out.get(name, [] if isinstance(value, dict) and "+append" in value else None),
                value,
                name,
            )
        return out
    if key in {"services", "clients"} and isinstance(base, list) and isinstance(patch, list):
        items = list(base)
        ids = {item["id"]: i for i, item in enumerate(items)}
        seen: set[str] = set()
        for item in patch:
            identity = item["id"]
            if identity in seen:
                raise ValueError(f"duplicate {key} id: {identity}")
            seen.add(identity)
            if identity in ids:
                i = ids[identity]
                items[i] = _merge(items[i], item)
            else:
                ids[identity] = len(items)
                items.append(item)
        return items
    return patch


def _service_map_data(repo_root: str | Path) -> tuple[dict[str, Any], dict[str, str]]:
    root = Path(repo_root)
    path = root / "axon.yaml"
    if not path.exists():
        path = root / "axon.yml"
    if not path.exists():
        raise FileNotFoundError(
            f"no axon.yaml in {root} — run `axon init --repo {root}` to draft one"
        )
    sources: dict[str, str] = {}

    def read(path: Path, active: set[Path], base: dict[str, Any] | None = None) -> dict[str, Any]:
        path = path.resolve()
        if path in active:
            raise ValueError(f"service map overlay cycle: {path}")
        raw = path.read_bytes()
        sources[str(path)] = hashlib.sha256(raw).hexdigest()
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            raise ValueError(f"service map must be a mapping: {path}")
        result: dict[str, Any] = dict(base or {})
        extends = data.pop("extends", [])
        if isinstance(extends, str):
            extends = [extends]
        if not isinstance(extends, list) or not all(isinstance(v, str) for v in extends):
            raise ValueError("extends must be a path or list of paths")
        for parent in extends:
            result = read(path.parent / parent, active | {path}, result)
        overrides = data.pop("overrides", None)
        result = _merge(result, data)
        if isinstance(overrides, dict):
            result = _merge(result, overrides)
        elif overrides is not None:
            paths = [overrides] if isinstance(overrides, str) else overrides
            if not isinstance(paths, list) or not all(isinstance(v, str) for v in paths):
                raise ValueError("overrides must be a mapping, path or list of paths")
            for overlay in paths:
                result = read(path.parent / overlay, active | {path}, result)
        return result

    return read(path, set()), sources


def service_map_fingerprint(repo_root: str | Path) -> str:
    """Hash every configuration source, including recursively loaded overlays."""
    _, sources = _service_map_data(repo_root)
    # Paths are relative so relocating a checkout does not invalidate its identity.
    root = Path(repo_root).resolve()
    entries = sorted(
        (str(Path(p).relative_to(root)) if Path(p).is_relative_to(root) else p, h)
        for p, h in sources.items()
    )
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def load_service_map(repo_root: str | Path) -> ServiceMap:
    data, _ = _service_map_data(repo_root)
    sm = ServiceMap.model_validate(data)
    if not sm.repo:
        sm.repo = Path(repo_root).resolve().name
    return sm


def dump_service_map(sm: ServiceMap) -> str:
    data = sm.model_dump(exclude_none=True)
    return str(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def _matches(path: str, pattern: str) -> bool:
    pat = pattern.strip().rstrip("/")
    if not pat:
        return False
    if pat == ".":
        return True
    if any(c in pat for c in "*?["):
        # "**/x" must also match "x" at the repo root (zero directories),
        # otherwise top-level .venv/node_modules slip past DEFAULT_IGNORE.
        pats = [pat, pat[3:]] if pat.startswith("**/") else [pat]
        return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path, p + "/*") for p in pats)
    return path == pat or path.startswith(pat + "/")


def is_ignored(sm: ServiceMap, rel_path: str) -> bool:
    return any(_matches(rel_path, p) for p in [*DEFAULT_IGNORE, *sm.ignore])


def owner_of(sm: ServiceMap, rel_path: str) -> tuple[str, str]:
    """Return (kind, service_id): kind is service|shared|client|unowned.

    Longest matching root wins so nested services override outer ones.
    """
    best: tuple[int, str, str] | None = None  # (pattern length, kind, id)

    def consider(patterns: list[str], kind: str, sid: str) -> None:
        nonlocal best
        for p in patterns:
            if _matches(rel_path, p):
                cand = (len(p), kind, sid)
                if best is None or cand[0] > best[0]:
                    best = cand

    for svc in sm.services:
        consider(svc.roots, "service", svc.id)
        for client in svc.clients:
            consider(client.paths, "client", svc.id)
    consider(sm.shared, "shared", "")
    if best is None:
        return ("unowned", "")
    return (best[1], best[2])
