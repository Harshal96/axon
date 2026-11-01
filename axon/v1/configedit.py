"""Local, reviewable round-trip edits to the effective service map.

Plans contain exact before/after bytes. Validation and optimistic concurrency
checks happen before any file is replaced; generated inputs are never edited.
"""

from __future__ import annotations

import copy
import difflib
import fcntl
import hashlib
import io
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from axon.v1.servicemap import (
    ServiceMap,
    _service_map_data,
    load_service_map,
    load_source_map,
)


class EditError(ValueError):
    def __init__(self, message: str, code: int = 2, candidates: Any = None):
        super().__init__(message)
        self.code = code
        self.candidates = candidates or []


@dataclass
class Ref:
    entity: str
    file: str
    yaml_path: tuple[str | int, ...]
    line: int
    value: Any


@dataclass
class EditOp:
    action: str
    entity: str
    value: dict[str, Any] = field(default_factory=dict)


@dataclass
class EditPlan:
    ops: list[EditOp]
    before: dict[str, str | None]
    after: dict[str, str]
    effects: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def diffs(self) -> dict[str, str]:
        return {
            path: "".join(
                difflib.unified_diff(
                    (self.before.get(path) or "").splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    fromfile=path,
                    tofile=path,
                )
            )
            for path, text in self.after.items()
            if text != self.before.get(path)
        }

    def as_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return {
            "ops": [asdict(op) for op in self.ops],
            "diffs": self.diffs,
            "effects": self.effects,
            "warnings": self.warnings,
        }


_LISTS = {
    "service": "services",
    "client": "clients",
    "link": "links",
    "suppress": "suppress",
    "call": "calls",
    "endpoint": "endpoints",
    "handler": "handlers",
}


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _render(data: Any) -> str:
    out = io.StringIO()
    _yaml().dump(data, out)
    return out.getvalue()


def _at(data: Any, path: tuple[str | int, ...]) -> Any:
    for key in path:
        data = data[key]
    return data


class ConfigEditor:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.sm = load_service_map(self.root)
        self.sources = load_source_map(self.root)
        self.before: dict[str, str | None] = {}
        self.documents: dict[str, Any] = {}
        for name in self.sources.hashes:
            self._load(Path(name))
        golden = self.root / "axon.golden.yaml"
        if golden.exists():
            self._load(golden)
        self.ops: list[EditOp] = []
        self.effects: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def _load(self, path: Path) -> Any:
        path = path.resolve()
        if not path.is_relative_to(self.root):
            raise EditError(f"config source outside repository: {path}")
        name = str(path)
        if name not in self.documents:
            text = path.read_text() if path.exists() else None
            self.before[name] = text
            self.documents[name] = _yaml().load(text or "{}") or CommentedMap()
        return self.documents[name]

    def generated(self, path: Path) -> bool:
        relative = str(path.relative_to(self.root))
        return (
            relative in self.sm.generated
            or "# axon:generated" in (self.before.get(str(path)) or "").splitlines()[:5]
        )

    def _owner(self, kind: str, identity: str) -> str:
        if kind == "service":
            return identity
        if "/" in identity:
            return identity.split("/", 1)[0]
        if kind == "client":
            return next(
                (s.id for s in self.sm.services if any(c.id == identity for c in s.clients)), ""
            )
        return ""

    def target(self, entity: str) -> tuple[Path, tuple[str | int, ...] | None]:
        kind, identity = entity.split(":", 1)
        owner = self._owner(kind, identity)
        root_path = self.root / ("axon.yaml" if (self.root / "axon.yaml").exists() else "axon.yml")
        root_doc = self.documents[str(root_path)]
        overrides = root_doc.get("overrides", [])
        paths = [overrides] if isinstance(overrides, str) else overrides
        if owner and isinstance(paths, list):
            for pattern in paths:
                if not isinstance(pattern, str) or not any(c in pattern for c in "*?["):
                    continue
                service = next((s for s in self.sm.services if s.id == owner), None)
                if service:
                    candidates = [
                        self.root / p.rstrip("/") / "axon.service.yaml"
                        for p in service.roots
                        if not any(c in p for c in "*?[")
                    ]
                    candidates = [p for p in candidates if p.relative_to(self.root).match(pattern)]
                    if candidates:
                        return sorted(candidates)[0], None
        locations = self.sources.entities.get(entity, [])
        for location in reversed(locations):
            path = Path(location.file)
            if not self.generated(path):
                return path, location.yaml_path
            # A later generated contribution masks earlier editable declarations.
            break
        if locations:
            last_source = self.sources.order.index(locations[-1].file)
            if isinstance(paths, list):
                for pattern in reversed(paths):
                    if isinstance(pattern, str) and not any(c in pattern for c in "*?["):
                        path = (root_path.parent / pattern).resolve()
                        self._load(path)
                        position = (
                            self.sources.order.index(str(path))
                            if str(path) in self.sources.order
                            else len(self.sources.order)
                        )
                        if not self.generated(path) and position > last_source:
                            return path, None
            if (
                not self.generated(root_path)
                and self.sources.order.index(str(root_path)) > last_source
            ):
                return root_path, None
            raise EditError(
                "generated config has no editable override; add a root overrides file", 3
            )
        if self.generated(root_path):
            raise EditError("generated config has no editable override", 3)
        return root_path, None

    def _container(self, document: Any, entity: str) -> list[Any]:
        kind, identity = entity.split(":", 1)
        owner = self._owner(kind, identity)
        if kind not in _LISTS:
            raise EditError(f"unknown entity kind: {kind}")
        if kind != "service" and owner:
            services = document.setdefault("services", [])
            service = next((s for s in services if s.get("id") == owner), None)
            if service is None:
                service = CommentedMap(id=owner)
                services.append(service)
            return cast(list[Any], service.setdefault(_LISTS[kind], []))
        return cast(list[Any], document.setdefault(_LISTS[kind], []))

    def edit(self, action: str, entity: str, value: dict[str, Any] | None = None) -> None:
        rows: list[Any] | None
        kind, identity = entity.split(":", 1)
        owner = self._owner(kind, identity)
        if kind != "service" and owner and owner not in {s.id for s in self.sm.services}:
            raise EditError(f"unknown owning service: {owner}")
        short = identity.split("/")[-1]
        path, yaml_path = self.target(entity)
        document = self._load(path)
        self.ops.append(EditOp(action, entity, value or {}))
        if action == "remove":
            # Remove every editable contribution, then mask any inherited generated row.
            inherited = False
            for location in reversed(self.sources.entities.get(entity, [])):
                source = Path(location.file)
                if self.generated(source):
                    inherited = True
                    continue
                parent = _at(self.documents[str(source)], location.yaml_path[:-1])
                parent[:] = [row for row in parent if row.get("id") != short]
            if inherited:
                rows = self._container(document, entity)
                rows[:] = [row for row in rows if row.get("id") != short]
                rows.append(CommentedMap({"id": short, "+delete": True}))
            return
        if action == "add" and entity in self.sources.entities:
            raise EditError(f"entity already exists: {entity}")
        if yaml_path and action == "patch":
            row = _at(document, yaml_path)
            rows = None
        else:
            rows = self._container(document, entity)
            row = next((r for r in rows if r.get("id") == short), None)
        if row is None:
            assert rows is not None
            row = CommentedMap(id=short)
            rows.append(row)
        row.pop("+delete", None)
        row.update(value or {})

    def _validate(self, after: dict[str, str]) -> None:
        from collections import Counter

        from axon.v1.doctor import doctor

        try:
            data, _ = _service_map_data(self.root, contents=after)
            proposed = ServiceMap.model_validate(data)
            baseline = doctor(self.root, service_map=self.sm)
            checked = doctor(self.root, service_map=proposed)

            def errors(report: dict[str, Any]) -> Counter[tuple[str, str]]:
                return Counter(
                    (issue["code"], issue["message"])
                    for issue in report["issues"]
                    if issue["severity"] == "error"
                )

            added = errors(checked) - errors(baseline)
            if added:
                raise EditError(
                    "new doctor errors: "
                    + "; ".join(f"{code}: {message}" for code, message in sorted(added))
                )
        except (ValueError, OSError, TypeError) as exc:
            raise EditError(str(exc)) from exc

    def plan(self) -> EditPlan:
        after = {name: _render(document) for name, document in self.documents.items()}
        # Avoid reserializing untouched files at all (including generated inputs).
        for name, text in self.before.items():
            if text is not None and _yaml().load(text) == self.documents[name]:
                after[name] = text
        self._validate(after)
        for name in after:
            if self.generated(Path(name)) and after[name] != self.before[name]:
                raise EditError(f"refusing to edit generated file: {name}")
        return EditPlan(self.ops[:], self.before.copy(), after, self.effects[:], self.warnings[:])

    def patch(self, entity: str, value: dict[str, Any]) -> EditPlan:
        if entity not in self.sources.entities:
            raise EditError(f"unknown entity: {entity}")
        previous = copy.deepcopy(self.documents)
        count = len(self.ops)
        try:
            self.edit("patch", entity, value)
            return self.plan()
        except EditError:
            self.documents = previous
            del self.ops[count:]
            raise

    def add(self, entity: str, value: dict[str, Any]) -> EditPlan:
        self.edit("add", entity, value)
        return self.plan()

    def remove(self, entity: str) -> EditPlan:
        if entity not in self.sources.entities:
            raise EditError(f"unknown entity: {entity}")
        self.edit("remove", entity)
        return self.plan()

    def _check_preconditions(self, plan: EditPlan) -> None:
        golden = self.root / "axon.golden.yaml"
        if str(golden) not in self.before and golden.exists():
            raise EditError("golden dependencies changed since preview", 4)
        if plan.before != self.before or not set(plan.after) <= set(self.before):
            raise EditError("plan dependency preconditions changed", 4)
        for name, text in plan.before.items():
            path = Path(name)
            if not path.resolve().is_relative_to(self.root):
                raise EditError(f"config source outside repository: {path}")
            if (path.read_text() if path.exists() else None) != text:
                raise EditError(f"config changed since preview: {name}", 4)
        try:
            current = load_source_map(self.root)
        except (OSError, ValueError) as exc:
            raise EditError(f"config dependencies changed since preview: {exc}", 4) from exc
        if current.hashes != self.sources.hashes:
            raise EditError("config dependencies changed since preview", 4)
        for name, text in plan.after.items():
            if self.generated(Path(name)) and text != self.before[name]:
                raise EditError(f"refusing to edit generated file: {name}")

    def apply(self, plan: EditPlan) -> None:
        # All Axon editors for this repository share a process-safe lock.
        identity = hashlib.sha256(str(self.root).encode()).hexdigest()
        lock_path = Path(tempfile.gettempdir()) / f"axon-config-edit-{identity}.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._apply_locked(plan)

    def _apply_locked(self, plan: EditPlan) -> None:
        self._check_preconditions(plan)
        self._validate(plan.after)
        staged: list[tuple[Path, Path]] = []
        replaced: list[Path] = []
        try:
            for name in plan.diffs:
                path = Path(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".axon-")
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(plan.after[name])
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.exists():
                    os.chmod(temporary_name, path.stat().st_mode & 0o777)
                staged.append((path, Path(temporary_name)))
            # Staging may take time: recheck every source, including glob membership.
            self._check_preconditions(plan)
            self._validate(plan.after)
            for path, temporary in staged:
                os.replace(temporary, path)
                replaced.append(path)
        except OSError:
            for path in replaced:
                original = plan.before[str(path)]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    fd, rollback = tempfile.mkstemp(dir=path.parent, prefix=".axon-rollback-")
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as stream:
                            stream.write(original)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(rollback, path)
                    finally:
                        Path(rollback).unlink(missing_ok=True)
            raise
        finally:
            for _, temporary in staged:
                temporary.unlink(missing_ok=True)


def config_refs(root: str | Path, entity: str) -> list[Ref]:
    editor = ConfigEditor(root)
    kind, identity = entity.split(":", 1)
    refs: list[Ref] = []
    prefixes: list[str] = []
    if kind == "client":
        for service in editor.sm.services:
            for client in service.clients:
                if client.id == identity:
                    prefixes = [*client.packages, *client.factories]
    endpoint_service, endpoint_method, endpoint_path = "", "", ""
    if kind == "endpoint" and identity.startswith("endpoint::"):
        _, _, endpoint_service, operation = identity.split("::", 3)
        endpoint_method, _, endpoint_path = operation.partition(":")
    keys = (
        {"target_service", "to_service", "from_service", "service"}
        if kind == "service"
        else {"from_client"}
        if kind == "client"
        else {"api", "from_api", "to_path", "path"}
    )

    def walk(value: Any, path: tuple[str | int, ...], file: str, scope: str = "") -> None:
        if isinstance(value, dict):
            if len(path) >= 2 and path[-2] == "services":
                scope = value.get("id", scope)
            line = value.lc.line + 1 if hasattr(value, "lc") else 1
            if kind == "endpoint" and endpoint_path:
                caller_api = f"{endpoint_method} {endpoint_path}"
                selector_hit = (
                    value.get("service") == endpoint_service
                    and value.get("method", "GET").upper() == endpoint_method
                    and value.get("path") == endpoint_path
                )
                target_hit = (
                    value.get("to_service") == endpoint_service
                    and value.get("to_method", "GET").upper() == endpoint_method
                    and value.get("to_path") == endpoint_path
                )
                scoped_hit = scope == endpoint_service and (
                    value.get("api") == caller_api
                    or value.get("from_api") == caller_api
                    or (
                        value.get("method", "").upper() == endpoint_method
                        and value.get("path") == endpoint_path
                    )
                )
                if selector_hit or target_hit or scoped_hit:
                    refs.append(Ref(entity, file, path, line, dict(value)))
            for key, item in value.items():
                package_hit = (
                    kind == "client"
                    and key == "call_name"
                    and isinstance(item, str)
                    and any(item == p or item.startswith(p + ".") for p in prefixes)
                )
                if (
                    package_hit
                    or (key in keys and item == identity)
                    or (
                        key == "id"
                        and item == identity
                        and (
                            (kind == "service" and len(path) >= 2 and path[-2] == "services")
                            or (kind == "client" and len(path) >= 2 and path[-2] == "clients")
                        )
                    )
                ):
                    refs.append(
                        Ref(
                            entity,
                            file,
                            (*path, key),
                            value.lc.line + 1 if hasattr(value, "lc") else 1,
                            item,
                        )
                    )
                walk(item, (*path, key), file, scope)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, (*path, i), file, scope)

    for file, data in editor.documents.items():
        walk(data, (), file)
    if kind == "service":
        for key, locations in editor.sources.entities.items():
            if key.split(":", 1)[-1].startswith(identity + "/"):
                for location in locations:
                    refs.append(
                        Ref(
                            entity,
                            location.file,
                            location.yaml_path,
                            location.line,
                            dict(_at(editor.documents[location.file], location.yaml_path)),
                        )
                    )
    return refs
