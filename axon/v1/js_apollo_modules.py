"""Bind cached Apollo expressions against the modules present in this build."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Any

from axon.v1.facts import FileFacts
from axon.v1.graphql_inputs import DocumentSource, InvocationInput, ResolverBinding, ServerInput
from axon.v1.js_modules import find_module
from axon.v1.models import symbol_node_id


@dataclass
class _Value:
    file: FileFacts
    key: str
    data: dict[str, Any]

    @property
    def kind(self) -> str:
        return str(self.data["kind"])

    @property
    def identity(self) -> tuple[str, str]:
        return self.file.path, self.key


class _Scope:
    """Tracks the source files consulted for one execution or registration."""

    def __init__(self, files: dict[str, FileFacts]):
        self.files = files
        self.dependencies: set[str] = set()
        self._properties: dict[tuple[str, str], tuple[bool, dict[str, tuple[str, int]]]] = {}

    def resolve(self, file: FileFacts, key: str) -> _Value | None:
        for _ in range(32):
            self.dependencies.add(file.path)
            data = file.apollo.get("nodes", {}).get(key)
            if not data:
                return None
            if data["kind"] != "import":
                return _Value(file, key, data)
            module = data["module"]
            if module.endswith((".graphql", ".gql")):
                path = posixpath.normpath(posixpath.join(posixpath.dirname(file.path), module))
                self.dependencies.add(path)
                target = self.files.get(path)
                if target and target.graphql_document and data["export"] == "default":
                    return _Value(target, "", {"kind": "document"})
                return None
            target = find_module(file, module, self.files)
            if target is None:
                return None
            file = target
            if qualified := file.js_function_exports.get(data["export"]):
                self.dependencies.add(file.path)
                return _Value(file, qualified, {"kind": "symbol", "name": qualified})
            key = file.apollo.get("exports", {}).get(data["export"], "")
        return None

    def options(self, value: _Value) -> _Value | None:
        return self.resolve(value.file, value.data["options"])

    def properties(self, obj: _Value | None) -> tuple[bool, dict[str, tuple[str, int]]]:
        """Index a map once. Spreads may override preceding entries or introduce new ones."""
        if obj is None or obj.kind != "object":
            return True, {}
        if obj.identity not in self._properties:
            entries: dict[str, tuple[str, int]] = {}
            spread = False
            for key, child, line in reversed(obj.data["entries"]):
                if not key:
                    spread = True
                elif key in entries:
                    # Two explicit registrations for a field are ambiguous.
                    entries[key] = ("", entries[key][1])
                else:
                    entries[key] = ("" if spread else child, line)
            self._properties[obj.identity] = spread, entries
        return self._properties[obj.identity]

    def entry(self, obj: _Value | None, name: str) -> tuple[bool, _Value | None]:
        spread, entries = self.properties(obj)
        if obj and name in entries:
            return True, self.resolve(obj.file, entries[name][0])
        return spread, None

    def prop(self, obj: _Value | None, name: str) -> _Value | None:
        return self.entry(obj, name)[1]

    @staticmethod
    def string(value: _Value | None) -> str:
        if value and value.kind == "string":
            return str(value.data["value"])
        if (
            value
            and value.kind == "template"
            and not value.data["parts"]
            and not value.data["tagged"]
        ):
            return str(value.data["text"])
        return ""

    def documents(self, value: _Value | None, *, schema: bool) -> tuple[list[DocumentSource], bool]:
        documents: list[DocumentSource] = []
        visited: set[tuple[str, str]] = set()
        active: set[tuple[str, str]] = set()

        def collect(item: _Value | None) -> bool:
            if item is None or item.identity in active or len(active) >= 48:
                return False
            if item.identity in visited:
                return True
            data = item.data
            if item.kind == "document" and item.file.graphql_document:
                documents.append(item.file.graphql_document)
            elif item.kind == "string" and schema:
                documents.append(
                    DocumentSource(item.file.path, data["value"], data["line"], data["column"])
                )
            elif item.kind == "template" and (schema or data["tagged"]):
                if data["text"].strip():
                    documents.append(
                        DocumentSource(item.file.path, data["text"], data["line"], data["column"])
                    )
                active.add(item.identity)
                parts_ok = all(collect(self.resolve(item.file, key)) for key in data["parts"])
                active.remove(item.identity)
                if not parts_ok:
                    return False
            elif schema and item.kind == "array":
                active.add(item.identity)
                # Retain other declared SDL files even if an array entry is computed.
                results = [collect(self.resolve(item.file, key)) for key in data["items"]]
                active.remove(item.identity)
                if not all(results):
                    return False
            else:
                return False
            visited.add(item.identity)
            return True

        complete = collect(value)
        return (documents if schema or complete else []), complete

    def resolvers(self, repo_id: str, value: _Value | None) -> list[ResolverBinding]:
        maps = (
            [self.resolve(value.file, key) for key in value.data["items"]]
            if value and value.kind == "array"
            else [value]
        )
        known_maps = [item for item in maps if item is not None and item.kind == "object"]
        if len(known_maps) != len(maps):
            return []
        result: list[ResolverBinding] = []
        roots = {name for item in known_maps for name in self.properties(item)[1]}
        for root in sorted(roots):
            # A dynamic root in any merged resolver map might redefine this
            # entire type. An opaque field in a literal root only obscures the
            # corresponding coordinate, including coordinates from other maps.
            entries = [self.entry(item, root) for item in known_maps]
            definitions = [fields for present, fields in entries if present]
            field_maps = [fields for fields in definitions if fields and fields.kind == "object"]
            if len(field_maps) != len(definitions):
                continue
            names = {name for fields in field_maps for name in self.properties(fields)[1]}
            for name in sorted(names):
                for fields in field_maps:
                    present, fn = self.entry(fields, name)
                    if not present:
                        continue
                    declaration = self.properties(fields)[1].get(name)
                    line = declaration[1] if declaration else fields.data["line"]
                    symbol = (
                        symbol_node_id(repo_id, fn.file.path, fn.data["name"])
                        if fn and fn.kind == "symbol"
                        else ""
                    )
                    result.append(ResolverBinding(f"{root}.{name}", symbol, fields.file.path, line))
        return result

    def invocation(
        self, repo_id: str, file: FileFacts, call: dict[str, Any]
    ) -> InvocationInput | None:
        client = self.resolve(file, call["client"])
        if client is None or client.kind != "client":
            return None
        config = self.options(client)
        has_link, link = self.entry(config, "link")
        if has_link:
            transport = self.options(link) if link and link.kind == "link" else None
        else:
            transport = config
        uri = self.string(self.prop(transport, "uri"))
        options = self.resolve(file, call["options"])
        has_context, context = self.entry(options, "context")
        if has_context and transport is not None:
            has_uri, context_uri = self.entry(context, "uri")
            if has_uri:
                uri = self.string(context_uri)
        kind = call["kind"]
        documents, _ = self.documents(self.prop(options, kind), schema=False)
        has_policy, policy = self.entry(options, "fetchPolicy")
        if kind == "query" and not has_policy:
            defaults = self.prop(config, "defaultOptions")
            policy = self.prop(self.prop(defaults, "query"), "fetchPolicy")
        return InvocationInput(
            symbol_node_id(repo_id, file.path, call["caller"]),
            file.path,
            call["line"],
            call["column"],
            kind,
            uri,
            documents,
            sorted(self.dependencies),
            cache_only=kind == "query" and self.string(policy) == "cache-only",
        )


def apollo_inputs(
    repo_id: str, files: list[FileFacts], owners: dict[str, tuple[str, str]]
) -> tuple[list[ServerInput], list[InvocationInput]]:
    modules = {f.path: f for f in files}
    servers: list[ServerInput] = []
    calls: list[InvocationInput] = []
    mounts: dict[tuple[str, str], set[str]] = {}
    mount_sources: dict[tuple[str, str], set[str]] = {}
    for file in files:
        for mount in file.apollo.get("mounts", []):
            scope = _Scope(modules)
            value = scope.resolve(file, mount["server"])
            if value and value.kind == "server":
                mounts.setdefault(value.identity, set()).add(mount["path"])
                mount_sources.setdefault(value.identity, set()).update(scope.dependencies)
        for call in file.apollo.get("calls", []):
            if invocation := _Scope(modules).invocation(repo_id, file, call):
                calls.append(invocation)
    for file in files:
        for key in file.apollo.get("servers", []):
            scope = _Scope(modules)
            server = scope.resolve(file, key)
            if server is None:
                continue
            config = scope.options(server)
            docs, complete = scope.documents(scope.prop(config, "typeDefs"), schema=True)
            resolvers = scope.resolvers(repo_id, scope.prop(config, "resolvers"))
            servers.append(
                ServerInput(
                    owners.get(file.path, ("", ""))[1],
                    file.path,
                    server.data["line"],
                    docs,
                    resolvers,
                    sorted(mounts.get(server.identity, set())),
                    sorted(scope.dependencies | mount_sources.get(server.identity, set())),
                    complete,
                )
            )
    return servers, calls
