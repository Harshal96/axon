"""Local configuration maintenance. Every mutation previews by default."""

from __future__ import annotations

import functools
import inspect
import json
import re
from collections.abc import Callable
from typing import Any

import typer

from axon.v1.configedit import ConfigEditor, EditError, EditPlan
from axon.v1.maintenance import move_client, remove_client, remove_service, rename_entity

service_app = typer.Typer(help="Maintain service ownership")
client_app = typer.Typer(help="Maintain generated/SDK client declarations")
_groups = {"service": service_app, "client": client_app}


def mutation(group: typer.Typer, name: str) -> Callable[..., Any]:
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(function)
        def command(*args: Any, **kwargs: Any) -> None:
            repo = kwargs.pop("repo")
            apply = kwargs.pop("apply")
            dry_run = kwargs.pop("dry_run")
            impact = kwargs.pop("impact")
            json_output = kwargs.pop("json_output")
            try:
                if apply and dry_run:
                    raise EditError("choose --dry-run or --apply")
                editor = ConfigEditor(repo)
                plan = function(editor=editor, *args, **kwargs)
                data = plan.as_dict()
                if impact:
                    from axon.v1.shadow import preview_impact

                    data["impact"] = preview_impact(editor.root, plan)
                if apply:
                    editor.apply(plan)
                data["applied"] = apply
                if json_output:
                    typer.echo(json.dumps(data))
                else:
                    for diff in data["diffs"].values():
                        typer.echo(diff, nl=False)
                    for warning in data["warnings"]:
                        typer.echo(f"warning: {warning}")
                    typer.echo("Applied." if apply else "Dry run; use --apply to write.")
            except (ValueError, OSError, KeyError) as exc:
                code = exc.code if isinstance(exc, EditError) else 2
                data = {"error": str(exc), "candidates": getattr(exc, "candidates", [])}
                typer.echo(json.dumps(data) if json_output else str(exc))
                raise typer.Exit(code) from exc

        signature = inspect.signature(function)
        parameters = [p for key, p in signature.parameters.items() if key != "editor"]
        annotations = dict(function.__annotations__)
        annotations.pop("editor", None)
        for key, annotation, default in (
            ("repo", str, typer.Option(".", "--repo")),
            ("apply", bool, typer.Option(False, "--apply")),
            ("dry_run", bool, typer.Option(False, "--dry-run")),
            ("impact", bool, typer.Option(False, "--impact")),
            ("json_output", bool, typer.Option(False, "--json")),
        ):
            parameters.append(
                inspect.Parameter(
                    key, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation
                )
            )
            annotations[key] = annotation
        command.__signature__ = signature.replace(parameters=parameters)  # type: ignore[attr-defined]
        command.__annotations__ = annotations
        group.command(name)(command)
        return command

    return decorate


def _values(values: list[str]) -> dict[str, Any]:
    result = {}
    for entry in values:
        key, separator, value = entry.partition("=")
        if not separator or not key or "." in key:
            raise EditError("--set requires field=JSON (or field=text)")
        try:
            result[key] = json.loads(value)
        except json.JSONDecodeError:
            result[key] = value
    return result


@mutation(service_app, "add")
def service_add(
    editor: ConfigEditor,
    identity: str,
    root: list[str] = typer.Option([], "--root"),
    name: str = typer.Option("", "--name"),
    values: list[str] = typer.Option([], "--set"),
) -> EditPlan:
    return editor.add(f"service:{identity}", {"name": name, "roots": root, **_values(values)})


def _patch(kind: str) -> Callable[..., Any]:
    def patch(
        editor: ConfigEditor,
        identity: str,
        values: list[str] = typer.Option([], "--set"),
        add_package: list[str] = typer.Option([], "--add-package"),
    ) -> EditPlan:
        changes = _values(values)
        if add_package:
            if kind != "client":
                raise EditError("--add-package applies to clients")
            from axon.v1.maintenance import _client

            _, client = _client(editor, identity)
            changes["packages"] = list(dict.fromkeys([*client.packages, *add_package]))
        if not changes:
            raise EditError("provide --set field=value or --add-package")
        if "id" in changes:
            raise EditError("use rename to change identity and update references")
        return editor.patch(f"{kind}:{identity}", changes)

    return patch


mutation(service_app, "patch")(_patch("service"))
mutation(client_app, "patch")(_patch("client"))


@mutation(service_app, "remove")
def service_remove(
    editor: ConfigEditor,
    identity: str,
    ignore_code: bool = typer.Option(False, "--ignore-code"),
    leave_unowned: bool = typer.Option(False, "--leave-unowned"),
    move_clients_to: str = typer.Option("", "--move-clients-to"),
    drop_clients: bool = typer.Option(False, "--drop-clients"),
    cascade_clients: bool = typer.Option(False, "--cascade-clients"),
) -> EditPlan:
    return remove_service(
        editor,
        identity,
        ignore_code=ignore_code,
        leave_unowned=leave_unowned,
        move_clients_to=move_clients_to,
        drop_clients=drop_clients,
        cascade_clients=cascade_clients,
    )


def _rename(kind: str) -> Callable[..., Any]:
    def rename(editor: ConfigEditor, old: str, new: str) -> EditPlan:
        return rename_entity(editor, f"{kind}:{old}", new)

    return rename


mutation(service_app, "rename")(_rename("service"))
mutation(client_app, "rename")(_rename("client"))


@mutation(client_app, "add")
def client_add(
    editor: ConfigEditor,
    name: str,
    service: str = typer.Option("", "--service"),
    target: str = typer.Option("", "--target"),
) -> EditPlan:
    from axon.v1.client_resolve import resolve_client

    proposal = resolve_client(editor.root, name, service, target)
    if proposal.status != "ready":
        raise EditError(
            "; ".join(proposal.conflicts) or "client proposal needs user choice",
            3,
            proposal.model_dump(),
        )
    assert proposal.client is not None
    path, _ = editor.target(f"service:{proposal.service}")
    document = editor._load(path)
    rows = document.setdefault("services", [])
    row = next((s for s in rows if s.get("id") == proposal.service), None)
    if row is None:
        row = {"id": proposal.service}
        rows.append(row)
    row.setdefault("clients", []).append(proposal.client.model_dump())
    editor.effects.append({"proposal": proposal.model_dump()})
    editor.warnings.extend(proposal.warnings)
    return editor.plan()


@mutation(client_app, "remove")
def client_remove(
    editor: ConfigEditor,
    identity: str,
    force: bool = typer.Option(False, "--force"),
    cascade_links: bool = typer.Option(False, "--cascade-links"),
    cascade_golden: bool = typer.Option(False, "--cascade-golden"),
) -> EditPlan:
    return remove_client(
        editor, identity, force=force, cascade_links=cascade_links, cascade_golden=cascade_golden
    )


@mutation(client_app, "move")
def client_move(
    editor: ConfigEditor, identity: str, to: str = typer.Option(..., "--to")
) -> EditPlan:
    return move_client(editor, identity, to)


def _entity(kind: str, identity: str, service: str) -> str:
    return f"{kind}:{service + '/' if service else ''}{identity}"


def _id(identity: str, *parts: str) -> str:
    slug = identity or re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")
    if not slug:
        raise EditError("entry id is empty")
    return slug


link_app = typer.Typer(help="Redirect existing outbound call facts")
suppress_app = typer.Typer(help="Suppress false outbound edges")
call_app = typer.Typer(help="Declare evidenced outbound calls")
endpoint_app = typer.Typer(help="Declare routes missed by static parsing")
handler_app = typer.Typer(help="Correct endpoint handler bindings")
_groups.update(
    link=link_app, suppress=suppress_app, call=call_app, endpoint=endpoint_app, handler=handler_app
)


@mutation(link_app, "add")
def link_add(
    editor: ConfigEditor,
    identity: str = typer.Argument(""),
    service: str = typer.Option("", "--service"),
    from_client: str = typer.Option("", "--from-client"),
    call_name: str = typer.Option("", "--call-name"),
    url_contains: str = typer.Option("", "--url-contains"),
    to_service: str = typer.Option(..., "--to-service"),
    to_method: str = typer.Option("GET", "--to-method"),
    to_path: str = typer.Option(..., "--to-path"),
) -> EditPlan:
    if not call_name and not url_contains:
        raise EditError("link requires --call-name or --url-contains")
    identity = _id(identity, service, call_name, to_service, to_path)
    return editor.add(
        _entity("link", identity, service),
        {
            "call_name": call_name,
            "url_contains": url_contains,
            "from_client": from_client,
            "to_service": to_service,
            "to_method": to_method.upper(),
            "to_path": to_path,
        },
    )


@mutation(suppress_app, "add")
def suppress_add(
    editor: ConfigEditor,
    identity: str = typer.Argument(""),
    service: str = typer.Option(..., "--service"),
    from_api: str = typer.Option("", "--from-api"),
    to_service: str = typer.Option("", "--to-service"),
    from_client: str = typer.Option("", "--from-client"),
    reason: str = typer.Option(..., "--reason"),
) -> EditPlan:
    if not from_api and not to_service and not from_client:
        raise EditError("suppression requires a matching API, service, or client")
    return editor.add(
        _entity("suppress", _id(identity, service, from_api, to_service), service),
        {
            "from_api": from_api,
            "to_service": to_service,
            "from_client": from_client,
            "reason": reason,
        },
    )


@mutation(call_app, "add")
def call_add(
    editor: ConfigEditor,
    identity: str = typer.Argument(""),
    service: str = typer.Option(..., "--service"),
    from_api: str = typer.Option(..., "--from-api"),
    to_service: str = typer.Option(..., "--to-service"),
    to_method: str = typer.Option("GET", "--to-method"),
    to_path: str = typer.Option(..., "--to-path"),
    evidence: str = typer.Option(..., "--evidence"),
) -> EditPlan:
    file, separator, line_text = evidence.rpartition(":")
    if not separator or not line_text.isdigit():
        raise EditError("--evidence requires file:line")
    path = (editor.root / file).resolve()
    if not path.is_relative_to(editor.root):
        raise EditError("evidence must be inside the repository")
    lines = path.read_text().splitlines()
    line = int(line_text)
    if not 1 <= line <= len(lines):
        raise EditError("evidence line does not exist")
    return editor.add(
        _entity("call", _id(identity, service, from_api, to_service), service),
        {
            "from_api": from_api,
            "to": {"service": to_service, "method": to_method.upper(), "path": to_path},
            "evidence": {
                "file": str(path.relative_to(editor.root)),
                "line": line,
                "text": lines[line - 1],
            },
        },
    )


@mutation(endpoint_app, "add")
def endpoint_add(
    editor: ConfigEditor,
    identity: str = typer.Argument(""),
    service: str = typer.Option(..., "--service"),
    method: str = typer.Option(..., "--method"),
    path: str = typer.Option(..., "--path"),
    handler: str = typer.Option(..., "--handler"),
) -> EditPlan:
    return editor.add(
        _entity("endpoint", _id(identity, service, method, path), service),
        {"method": method.upper(), "path": path, "handler": handler},
    )


@mutation(handler_app, "set")
def handler_set(
    editor: ConfigEditor,
    identity: str = typer.Argument(""),
    service: str = typer.Option(..., "--service"),
    api: str = typer.Option(..., "--api"),
    handler: str = typer.Option(..., "--handler"),
) -> EditPlan:
    entity = _entity("handler", _id(identity, service, api), service)
    return (editor.patch if entity in editor.sources.entities else editor.add)(
        entity, {"api": api, "handler": handler}
    )


def _remove(kind: str) -> Callable[..., Any]:
    def remove(
        editor: ConfigEditor, identity: str, service: str = typer.Option("", "--service")
    ) -> EditPlan:
        return editor.remove(_entity(kind, identity, service))

    return remove


for _kind in ("link", "suppress", "call", "endpoint"):
    mutation(_groups[_kind], "remove")(_remove(_kind))


def register(app: typer.Typer) -> None:
    from cli.commands.diagnose import client_list, client_show, service_show

    service_app.command("show")(service_show)
    client_app.command("show")(client_show)
    client_app.command("list")(client_list)
    for name, group in _groups.items():
        app.add_typer(group, name=name)
