"""Deterministic config cascades; choices and code guards are explicit."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from axon.v1.configedit import ConfigEditor, EditError, EditPlan
from axon.v1.servicemap import ClientDecl, ServiceDecl


def _service(editor: ConfigEditor, identity: str) -> ServiceDecl:
    result = next((s for s in editor.sm.services if s.id == identity), None)
    if result is None:
        raise EditError(f"unknown service: {identity}")
    return result


def _client(editor: ConfigEditor, identity: str) -> tuple[ServiceDecl, ClientDecl]:
    for service in editor.sm.services:
        for client in service.clients:
            if client.id == identity:
                return service, client
    raise EditError(f"unknown client: {identity}")


def code_references(editor: ConfigEditor, client: ClientDecl) -> list[dict[str, Any]]:
    from axon.v1.indexer import collect_files
    from axon.v1.servicemap import owner_of

    refs = []
    needles = [*client.packages, *client.factories]
    for rel in collect_files(editor.root, editor.sm):
        if owner_of(editor.sm, rel)[0] == "client":
            continue
        path = editor.root / rel
        if path.suffix not in {".py", ".java", ".go", ".ts", ".tsx", ".js"}:
            continue
        for line, text in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if any(re.search(r"(?<![\w])" + re.escape(n) + r"(?![\w])", text) for n in needles):
                refs.append(
                    {
                        "file": rel,
                        "line": line,
                        "text": text.strip(),
                        "service": owner_of(editor.sm, rel)[1],
                    }
                )
    return refs


def _golden_service(editor: ConfigEditor, old: str, new: str = "") -> None:
    path = str(editor.root / "axon.golden.yaml")
    if path not in editor.documents:
        return
    data = editor.documents[path]
    kept = []
    for case in data.get("cases", []):
        api = case.get("api", {})
        if api.get("service") == old:
            if not new:
                continue
            api["service"] = new
        had_expectations = any(case.get(k) for k in ("expect", "forbid"))
        for key in ("expect", "forbid"):
            rows = case.get(key, [])
            if new:
                for row in rows:
                    if row.get("service") == old:
                        row["service"] = new
            else:
                case[key] = [row for row in rows if row.get("service") != old]
        if had_expectations and not any(case.get(k) for k in ("expect", "forbid")):
            continue
        kept.append(case)
    data["cases"] = kept


def _replace_links(editor: ConfigEditor, rows: list[dict[str, Any]]) -> None:
    """Legacy lists replace as a unit in their last contributing source."""
    for file in reversed(editor.sources.order):
        document = editor.documents[file]
        inline = document.get("overrides")
        container = inline if isinstance(inline, dict) and "links" in inline else document
        if "links" not in container:
            continue
        if editor.generated(Path(file)):
            # A later exact-path overlay is the only valid destination for replacement.
            root = editor.root / (
                "axon.yaml" if (editor.root / "axon.yaml").exists() else "axon.yml"
            )
            directives = editor.documents[str(root)].get("overrides", [])
            directives = [directives] if isinstance(directives, str) else directives
            for pattern in reversed(directives if isinstance(directives, list) else []):
                if any(c in pattern for c in "*?["):
                    continue
                target = (editor.root / pattern).resolve()
                if (
                    str(target) in editor.sources.order
                    and editor.sources.order.index(str(target)) > editor.sources.order.index(file)
                    and not editor.generated(target)
                ):
                    editor.documents[str(target)]["links"] = rows
                    return
            raise EditError("generated legacy links need a later editable overrides file", 3)
        container["links"] = rows
        return


def _corrections(editor: ConfigEditor, old: str, new: str = "", skip_owner: str = "") -> None:
    for service in editor.sm.services:
        if service.id == skip_owner or (service.id == old and not new):
            continue
        for collection, kind in (("links", "link"), ("calls", "call"), ("suppress", "suppress")):
            for row in getattr(service, collection):
                data = row.model_dump()
                target = data.get("to_service", (data.get("to") or {}).get("service"))
                target_repo = data.get("to_repo", (data.get("to") or {}).get("repo", ""))
                if target != old or (target_repo and target_repo != editor.sm.repo):
                    continue
                entity = f"{kind}:{service.id}/{row.id}"
                if not row.id:
                    raise EditError("unkeyed nested correction requires an id before cascading", 3)
                if new:
                    if "to_service" in data:
                        data["to_service"] = new
                    else:
                        data["to"]["service"] = new
                    editor.edit("patch", entity, data)
                else:
                    editor.edit("remove", entity)
    for link in editor.sm.links:
        target_matches = link.to_service == old and link.to_repo in {"", editor.sm.repo}
        if not target_matches and link.from_service != old:
            continue
        if not link.id:
            # Legacy unkeyed lists have replacement semantics. Patch the effective list
            # in the root, never mutate a generated input.
            rows = [
                row.model_dump()
                for row in editor.sm.links
                if new or (row.to_service != old and row.from_service != old)
            ]
            if new:
                for row in rows:
                    for key in ("to_service", "from_service"):
                        if row.get(key) == old:
                            row[key] = new
            _replace_links(editor, rows)
            break
        data = link.model_dump()
        if new:
            for key in ("to_service", "from_service"):
                if data.get(key) == old and (key != "to_service" or target_matches):
                    data[key] = new
            editor.edit("patch", f"link:{link.id}", data)
        else:
            editor.edit("remove", f"link:{link.id}")


def remove_service(
    editor: ConfigEditor,
    identity: str,
    *,
    ignore_code: bool = False,
    leave_unowned: bool = False,
    move_clients_to: str = "",
    drop_clients: bool = False,
    cascade_clients: bool = False,
) -> EditPlan:
    service = _service(editor, identity)
    if ignore_code and leave_unowned:
        raise EditError("--ignore-code and --leave-unowned are mutually exclusive")
    existing = [p for p in service.roots if list(editor.root.glob(p))]
    if existing and not (ignore_code or leave_unowned):
        raise EditError(
            "service roots still exist; choose --ignore-code or --leave-unowned",
            3,
            [{"roots": existing, "choices": ["ignore-code", "leave-unowned"]}],
        )
    if move_clients_to and drop_clients:
        raise EditError("choose --move-clients-to or --drop-clients")
    for client in service.clients:
        consumers = [r for r in code_references(editor, client) if r["service"] != identity]
        if consumers and not (move_clients_to or drop_clients):
            raise EditError(
                "clients have consumers outside the removed service; move or drop them",
                3,
                consumers,
            )
        if move_clients_to:
            _move(editor, client.id, move_clients_to)
    for other in editor.sm.services:
        if other.id == identity:
            continue
        for client in other.clients:
            if client.target_service == identity and not client.target_repo:
                editor.edit(
                    "remove" if cascade_clients else "patch",
                    f"client:{client.id}",
                    {} if cascade_clients else {"target_service": ""},
                )
                editor.effects.append(
                    {"client": client.id, "effect": "target removed; calls external"}
                )
    _corrections(editor, identity)
    _golden_service(editor, identity)
    editor.edit("remove", f"service:{identity}")
    if ignore_code:
        root = editor.root / ("axon.yaml" if (editor.root / "axon.yaml").exists() else "axon.yml")
        directives = editor.documents[str(root)].get("overrides", [])
        directives = [directives] if isinstance(directives, str) else directives
        for pattern in reversed(directives if isinstance(directives, list) else []):
            if not isinstance(pattern, str) or any(c in pattern for c in "*?["):
                continue
            candidate = (editor.root / pattern).resolve()
            editor._load(candidate)
            if not editor.generated(candidate):
                root = candidate
                break
        if editor.generated(root):
            raise EditError("ignore-code requires an editable root or global override", 3)
        editor.documents[str(root)]["ignore"] = list(
            dict.fromkeys([*editor.sm.ignore, *service.roots])
        )
    return editor.plan()


def _move(editor: ConfigEditor, identity: str, target: str) -> None:
    owner, client = _client(editor, identity)
    _service(editor, target)
    if owner.id == target:
        return
    editor.edit("remove", f"client:{identity}")
    # Client identity is repo-global; construct the target declaration explicitly.
    path, _ = editor.target(f"service:{target}")
    document = editor._load(path)
    services = document.setdefault("services", [])
    row = next((s for s in services if s.get("id") == target), None)
    if row is None:
        row = {"id": target}
        services.append(row)
    row.setdefault("clients", []).append(client.model_dump())


def move_client(editor: ConfigEditor, identity: str, target: str) -> EditPlan:
    _move(editor, identity, target)
    return editor.plan()


def _client_rule(rule: str, identity: str) -> bool:
    prefix = f"client:{identity}"
    return rule == prefix or rule.startswith(prefix + "(")


def _cascade_client_golden(editor: ConfigEditor, identity: str, before: Any) -> None:
    from axon.v1.golden import _matches
    from axon.v1.models import EdgeKind, NodeKind

    golden_path = str(editor.root / "axon.golden.yaml")
    document = editor.documents.get(golden_path)
    if not document:
        return

    def hits(snapshot: Any, case: Any, target: Any) -> list[Any]:
        endpoints = [
            node
            for node in snapshot.by_kind(NodeKind.API_ENDPOINT)
            if node.repo_id == editor.sm.repo and _matches(node, case["api"])
        ]
        if len(endpoints) != 1:
            return []
        evidence = target.get("evidence", {})
        handlers = snapshot.handlers_of(endpoints[0].id)
        closure = {sid for handler in handlers for sid in snapshot.call_closure(handler.id)}
        closure.update(handler.id for handler in handlers)
        return [
            edge
            for edge in snapshot.edges
            if edge.kind == EdgeKind.CALLS_API
            and edge.from_id in closure
            and edge.to_id != endpoints[0].id
            and edge.to_id in snapshot.nodes
            and _matches(snapshot.nodes[edge.to_id], target)
            and (
                not evidence
                or (
                    edge.evidence.file == evidence.get("file")
                    and edge.evidence.line == evidence.get("line")
                )
            )
        ]

    candidates = []
    for case in document.get("cases", []):
        for target in case.get("expect", []):
            support = hits(before, case, target)
            if support and all(_client_rule(edge.evidence.rule, identity) for edge in support):
                candidates.append((case, target))
    if not candidates:
        return
    after = _client_removal_shadow(editor)
    for case, target in candidates:
        if not hits(after, case, target):
            case["expect"].remove(target)
            editor.effects.append(
                {
                    "effect": "golden expectation removed",
                    "case": case.get("id", str(case["api"])),
                    "target": dict(target),
                    "client": identity,
                }
            )
    document["cases"] = [
        case for case in document.get("cases", []) if case.get("expect") or case.get("forbid")
    ]


def _client_removal_shadow(editor: ConfigEditor) -> Any:
    import shutil
    import tempfile

    from axon.v1.indexer import index_repo

    plan = editor.plan()
    with tempfile.TemporaryDirectory(prefix="axon-client-removal-") as temporary:
        shadow = Path(temporary).resolve() / editor.root.name
        shutil.copytree(
            editor.root,
            shadow,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "node_modules", "__pycache__", ".worktrees", ".axon", "dist"
            ),
        )
        for name in plan.diffs:
            path = shadow / Path(name).relative_to(editor.root)
            if not path.resolve().is_relative_to(shadow):
                raise EditError(f"client cascade config outside shadow: {name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(plan.after[name])
        snapshot, _ = index_repo(shadow, use_cache=False, workers=1)
        return snapshot


def remove_client(
    editor: ConfigEditor,
    identity: str,
    *,
    force: bool = False,
    cascade_links: bool = False,
    cascade_golden: bool = False,
) -> EditPlan:
    _, client = _client(editor, identity)
    from axon.v1.indexer import index_repo
    from axon.v1.models import EdgeKind

    before, _ = index_repo(editor.root, use_cache=False, workers=1)
    refs = code_references(editor, client)
    refs.extend(
        {
            "file": edge.evidence.file,
            "line": edge.evidence.line,
            "rule": edge.evidence.rule,
            "from_id": edge.from_id,
            "to_id": edge.to_id,
        }
        for edge in before.edges
        if edge.kind == EdgeKind.CALLS_API and _client_rule(edge.evidence.rule, identity)
    )
    if refs and not force:
        raise EditError("code still references client; --force stops tracking it", 4, refs)
    for service in editor.sm.services:
        for kind in ("links", "suppress"):
            for row in getattr(service, kind):
                if row.from_client == identity:
                    editor.edit(
                        "remove", f"{'link' if kind == 'links' else kind}:{service.id}/{row.id}"
                    )
    for link in editor.sm.links:
        matches = (
            any(
                link.call_name == n or link.call_name.startswith(n + ".")
                for n in [*client.packages, *client.factories]
            )
            if link.call_name
            else False
        )
        if link.from_client == identity or (matches and cascade_links):
            if not link.id:
                remaining = [
                    row.model_dump()
                    for row in editor.sm.links
                    if row.from_client != identity
                    and not (
                        cascade_links
                        and row.call_name
                        and any(
                            row.call_name == n or row.call_name.startswith(n + ".")
                            for n in [*client.packages, *client.factories]
                        )
                    )
                ]
                _replace_links(editor, remaining)
                break
            editor.edit("remove", f"link:{link.id}")
        elif matches:
            editor.warnings.append(f"link {link.id} matches client package; use --cascade-links")
    editor.edit("remove", f"client:{identity}")
    if cascade_golden:
        _cascade_client_golden(editor, identity, before)
    return editor.plan()


def rename_entity(editor: ConfigEditor, entity: str, new: str) -> EditPlan:
    kind, old = entity.split(":", 1)
    if f"{kind}:{new}" in editor.sources.entities:
        raise EditError(f"entity already exists: {kind}:{new}")
    if kind == "service":
        service = _service(editor, old)

        def rewrite_self(data: Any) -> None:
            if isinstance(data, dict):
                for key in ("target_service", "to_service", "from_service", "service"):
                    remote = data.get("target_repo", data.get("to_repo", data.get("repo", "")))
                    if data.get(key) == old and (not remote or remote == editor.sm.repo):
                        data[key] = new
                for child in data.values():
                    rewrite_self(child)
            elif isinstance(data, list):
                for child in data:
                    rewrite_self(child)

        # In-place rename preserves ordering; inherited/generated identities use tombstone + add.
        locations = editor.sources.entities[entity]
        if all(not editor.generated(Path(loc.file)) for loc in locations):
            for loc in locations:
                from axon.v1.configedit import _at

                declaration = _at(editor.documents[loc.file], loc.yaml_path)
                declaration["id"] = new
                rewrite_self(declaration)
        else:
            editor.edit("remove", entity)
            renamed = {**service.model_dump(), "id": new}
            rewrite_self(renamed)
            editor.edit("add", f"service:{new}", renamed)
        for owner in editor.sm.services:
            if owner.id == old:
                continue
            for client in owner.clients:
                if client.target_service == old and not client.target_repo:
                    editor.edit("patch", f"client:{client.id}", {"target_service": new})
        _corrections(editor, old, new, skip_owner=old)
        _golden_service(editor, old, new)
    elif kind == "client":
        owner, client = _client(editor, old)
        locations = editor.sources.entities[entity]
        if any(editor.generated(Path(loc.file)) for loc in locations):
            editor.edit("remove", entity)
            path, _ = editor.target(f"service:{owner.id}")
            document = editor._load(path)
            services = document.setdefault("services", [])
            declaration = next((s for s in services if s.get("id") == owner.id), None)
            if declaration is None:
                declaration = {"id": owner.id}
                services.append(declaration)
            declaration.setdefault("clients", []).append({**client.model_dump(), "id": new})
        else:
            for loc in locations:
                from axon.v1.configedit import _at

                _at(editor.documents[loc.file], loc.yaml_path)["id"] = new
        for service in editor.sm.services:
            for collection, singular in (("links", "link"), ("suppress", "suppress")):
                for correction in getattr(service, collection):
                    if correction.from_client == old:
                        editor.edit(
                            "patch",
                            f"{singular}:{service.id}/{correction.id}",
                            {"from_client": new},
                        )
        for link in editor.sm.links:
            if link.from_client == old and link.id:
                editor.edit("patch", f"link:{link.id}", {"from_client": new})
        for file, document in editor.documents.items():
            if editor.generated(Path(file)):
                continue

            def walk(value: Any) -> None:
                if isinstance(value, dict):
                    if value.get("from_client") == old:
                        value["from_client"] = new
                    for item in value.values():
                        walk(item)
                elif isinstance(value, list):
                    for item in value:
                        walk(item)

            walk(document)
    else:
        raise EditError("rename supports service or client")
    return editor.plan()
