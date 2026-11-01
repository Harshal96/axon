"""axon.yaml: the flexible per-repo service map.

Services may live anywhere in a monorepo. Ownership is declared with root
paths/globs, never derived from a build graph. Unknown keys are preserved
on round-trip via `extra` so teams can extend the file freely.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass, field
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

    id: str = ""
    from_service: str = ""
    from_client: str = ""

    # Match outbound calls by any of these, then point them at an endpoint.
    call_name: str = ""  # e.g. "PaymentClient.charge"
    url_contains: str = ""
    to_service: str
    to_method: str = "GET"
    to_path: str
    to_repo: str = ""


class ManualEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    text: str = Field(min_length=1)


class ManualTarget(BaseModel):
    model_config = ConfigDict(extra="allow")
    service: str
    method: str
    path: str
    repo: str = ""


class DeclaredCall(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    from_api: str
    to: ManualTarget
    evidence: ManualEvidence


class Suppression(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    from_api: str = ""
    to_service: str = ""
    from_client: str = ""
    reason: str = Field(min_length=1)


class DeclaredEndpoint(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    method: str
    path: str
    handler: str


class HandlerOverride(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    api: str
    handler: str


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
    links: list[ManualLink] = Field(default_factory=list)
    suppress: list[Suppression] = Field(default_factory=list)
    calls: list[DeclaredCall] = Field(default_factory=list)
    endpoints: list[DeclaredEndpoint] = Field(default_factory=list)
    handlers: list[HandlerOverride] = Field(default_factory=list)

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
    generated: list[str] = Field(default_factory=list)
    index: IndexOptions = Field(default_factory=IndexOptions)

    @field_validator("services")
    @classmethod
    def _unique_ids(cls, v: list[ServiceDecl]) -> list[ServiceDecl]:
        seen: set[str] = set()
        clients: set[str] = set()
        for s in v:
            if s.id in seen:
                raise ValueError(f"duplicate service id: {s.id}")
            seen.add(s.id)
            for client in s.clients:
                if client.id in clients:
                    raise ValueError(f"duplicate client id: {client.id}")
                clients.add(client.id)
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


@dataclass(frozen=True)
class SourceLocation:
    file: str
    yaml_path: tuple[str | int, ...]
    line: int
    op: str


@dataclass
class SourceMap:
    hashes: dict[str, str] = field(default_factory=dict)
    entities: dict[str, list[SourceLocation]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


def _track_sources(data: Any, path: Path, sources: SourceMap) -> None:
    def entries(rows: Any, kind: str, prefix: tuple[str | int, ...], service: str = "") -> None:
        if not isinstance(rows, list):
            return
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or not row.get("id"):
                continue
            identity = f"{kind}:{service + '/' if service else ''}{row['id']}"
            locations = sources.entities.setdefault(identity, [])
            line = row.lc.line + 1 if hasattr(row, "lc") else 1
            locations.append(
                SourceLocation(
                    str(path),
                    (*prefix, i),
                    line,
                    "delete" if row.get("+delete") else "patch" if locations else "define",
                )
            )
            if kind == "service":
                for nested, singular in (
                    ("clients", "client"),
                    ("links", "link"),
                    ("calls", "call"),
                    ("suppress", "suppress"),
                    ("endpoints", "endpoint"),
                    ("handlers", "handler"),
                ):
                    entries(
                        row.get(nested),
                        singular,
                        (*prefix, i, nested),
                        "" if nested == "clients" else row["id"],
                    )

    entries(data.get("services"), "service", ("services",))
    entries(data.get("links"), "link", ("links",))
    if isinstance(data.get("overrides"), dict):
        inline = data["overrides"]
        scratch = SourceMap(entities=sources.entities)
        # Inline paths must retain their overrides prefix.
        before = {key: len(value) for key, value in sources.entities.items()}
        _track_sources(inline, path, scratch)
        for key, locations in sources.entities.items():
            start = before.get(key, 0)
            locations[start:] = [
                SourceLocation(loc.file, ("overrides", *loc.yaml_path), loc.line, loc.op)
                for loc in locations[start:]
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
    keyed = {"services", "clients", "links", "suppress", "calls", "endpoints", "handlers"}
    if key in keyed and isinstance(patch, list):
        if not all(isinstance(item, dict) and item.get("id") for item in patch):
            if key in {"services", "clients"}:
                raise ValueError(f"{key} entries require id")
            return patch
        items = list(base or [])
        seen: set[str] = set()
        for item in patch:
            identity = item["id"]
            if identity in seen:
                raise ValueError(f"duplicate {key} id: {identity}")
            seen.add(identity)
            ids = {item.get("id"): i for i, item in enumerate(items)}
            if item.get("+delete") is True:
                if set(item) != {"id", "+delete"}:
                    raise ValueError("+delete accepts only id and +delete")
                items = [entry for entry in items if entry.get("id") != identity]
            elif identity in ids:
                i = ids[identity]
                items[i] = _merge(items[i], item)
            else:
                items.append(_merge({}, item))
        return items
    return patch


def _service_map_data(
    repo_root: str | Path,
    *,
    source_map: SourceMap | None = None,
    contents: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    root = Path(repo_root)
    contents = contents or {}
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
        raw = contents[str(path)].encode() if str(path) in contents else path.read_bytes()
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
            result = read_paths(path, parent, active, result)
        if source_map is not None:
            from ruamel.yaml import YAML

            source_map.order.append(str(path))
            _track_sources(YAML(typ="rt").load(raw) or {}, path, source_map)
        overrides = data.pop("overrides", None)
        result = _merge(result, data)
        if isinstance(overrides, dict):
            result = _merge(result, overrides)
        elif overrides is not None:
            paths = [overrides] if isinstance(overrides, str) else overrides
            if not isinstance(paths, list) or not all(isinstance(v, str) for v in paths):
                raise ValueError("overrides must be a mapping, path or list of paths")
            for overlay in paths:
                result = read_paths(path, overlay, active, result)
        return result

    def read_paths(
        path: Path, pattern: str, active: set[Path], result: dict[str, Any]
    ) -> dict[str, Any]:
        globbed = any(c in pattern for c in "*?[")
        paths = (
            sorted(
                set(path.parent.glob(pattern))
                | {
                    Path(p)
                    for p in contents
                    if Path(p).is_relative_to(path.parent)
                    and Path(p).relative_to(path.parent).match(pattern)
                }
            )
            if globbed
            else [path.parent / pattern]
        )
        for overlay in paths:
            if globbed:
                rel = str(overlay.resolve().relative_to(root.resolve()))
                if any(_matches(rel, p) for p in [*DEFAULT_IGNORE, *result.get("ignore", [])]):
                    continue
                raw = contents.get(str(overlay.resolve()))
                patch = yaml.safe_load(raw if raw is not None else overlay.read_bytes()) or {}
                allowed = {
                    s["id"]
                    for s in result.get("services", [])
                    if any(_matches(rel, p) for p in s.get("roots", []))
                }
                if set(patch) - {"services"} or any(
                    s.get("id") not in allowed for s in patch.get("services", [])
                ):
                    raise ValueError(f"overlay-scope: {rel} may only patch its owning service")
                # Team files cannot expand their own authority by changing roots.
                for service in patch.get("services", []):
                    if "roots" in service:
                        inherited = next(s for s in result["services"] if s["id"] == service["id"])
                        if service["roots"] != inherited.get("roots", []):
                            raise ValueError(f"overlay-scope: {rel} cannot change service roots")
            result = read(overlay, active | {path}, result)
        return result

    result = read(path, set())
    if source_map is not None:
        source_map.hashes.update(sources)
    return result, sources


def load_source_map(repo_root: str | Path) -> SourceMap:
    sources = SourceMap()
    _service_map_data(repo_root, source_map=sources)
    return sources


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


def owner_match(sm: ServiceMap, rel_path: str) -> tuple[str, str, str]:
    """Return (kind, service_id, matched root) using service-map ownership.

    Longest matching root wins so nested services override outer ones.
    """
    best: tuple[int, str, str, str] | None = None

    def consider(patterns: list[str], kind: str, sid: str) -> None:
        nonlocal best
        for p in patterns:
            if _matches(rel_path, p):
                cand = (len(p), kind, sid, p)
                if best is None or cand[0] > best[0]:
                    best = cand

    for svc in sm.services:
        consider(svc.roots, "service", svc.id)
        for client in svc.clients:
            consider(client.paths, "client", svc.id)
    consider(sm.shared, "shared", "")
    if best is None:
        return ("unowned", "", "")
    return (best[1], best[2], best[3])


def owner_of(sm: ServiceMap, rel_path: str) -> tuple[str, str]:
    """Return (kind, service_id), preserving the ownership lookup contract."""
    kind, service_id, _ = owner_match(sm, rel_path)
    return kind, service_id
