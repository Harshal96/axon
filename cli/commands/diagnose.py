"""Read-only graph diagnosis, local or through the configured server."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from urllib.parse import quote

import typer

from axon.v1 import diagnose
from axon.v1.servicemap import load_service_map, load_source_map
from cli.api_client import ApiClient, ApiError, resolve_config


def _run(
    action: str,
    repo: str,
    local: bool,
    api_url: str | None,
    json_output: bool,
    identity: str = "",
    service: str = "",
    kind: str = "",
) -> None:
    try:
        root = Path(repo).resolve()
        sm = load_service_map(root) if root.is_dir() else None
        repo_id = sm.repo if sm else repo
        if local:
            from axon.v1.indexer import index_repo

            snap, sm = index_repo(root)
            if action == "api-explain":
                result = diagnose.api_explain(snap, sm.repo, identity, service)
            elif action == "clients":
                result = {"clients": diagnose.client_list(snap, sm.repo)}
            elif action == "client":
                result = diagnose.client_show(snap, sm.repo, identity)
            elif action == "service":
                result = diagnose.service_show(snap, sm.repo, identity)
            else:
                from axon.v1.configedit import config_refs

                result = diagnose.graph_refs(snap, sm.repo, kind, identity)
                result["config_references"] = [
                    dataclasses.asdict(r) for r in config_refs(root, f"{kind}:{identity}")
                ]
            if action in {"client", "service"}:
                if action == "service":
                    declaration = next(s for s in sm.services if s.id == identity)
                    result["declaration"] = declaration.model_dump(mode="json")
                else:
                    declaration = next(
                        c for s in sm.services for c in s.clients if c.id == identity
                    )
                    result["declaration"] = declaration.model_dump(mode="json")
                sources = load_source_map(root)
                key = f"{action}:{identity}"
                result["sources"] = [dataclasses.asdict(s) for s in sources.entities.get(key, [])]
        else:
            base = f"/api/v1/repos/{quote(repo_id, safe='')}"
            params = {}
            if action == "api-explain":
                path = f"{base}/api-explain"
                params = {"api": identity, "service": service}
            elif action == "clients":
                path = f"{base}/clients"
            elif action in {"client", "service"}:
                path = f"{base}/{action}s/{quote(identity, safe='')}"
            else:
                path = f"{base}/refs/{quote(kind, safe='')}/{quote(identity, safe='')}"
            client = ApiClient(resolve_config(api_url))
            try:
                result = client._request("GET", path, params=params)
            finally:
                client.close()
        typer.echo(json.dumps(result, indent=None if json_output else 2))
        if result.get("status") == "ambiguous":
            raise typer.Exit(3)
    except (OSError, ValueError, KeyError, ApiError) as exc:
        typer.echo(json.dumps({"error": str(exc)}) if json_output else str(exc))
        raise typer.Exit(2) from exc


def service_show(
    id: str,
    repo: str = typer.Option(".", "--repo"),
    local: bool = typer.Option(False, "--local"),
    api_url: str | None = typer.Option(None, "--api-url"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show service graph coverage and declaration sources."""
    _run("service", repo, local, api_url, json_output, id)


def client_show(
    id: str,
    repo: str = typer.Option(".", "--repo"),
    local: bool = typer.Option(False, "--local"),
    api_url: str | None = typer.Option(None, "--api-url"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show client consumers and attributed edges."""
    _run("client", repo, local, api_url, json_output, id)


def client_list(
    repo: str = typer.Option(".", "--repo"),
    local: bool = typer.Option(False, "--local"),
    api_url: str | None = typer.Option(None, "--api-url"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List tracked clients."""
    _run("clients", repo, local, api_url, json_output)


def api_explain(
    api: str,
    service: str = typer.Option("", "--service"),
    repo: str = typer.Option(".", "--repo"),
    local: bool = typer.Option(False, "--local"),
    api_url: str | None = typer.Option(None, "--api-url"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Explain handlers, linked calls and unresolved graph evidence."""
    _run("api-explain", repo, local, api_url, json_output, api, service)


def refs(
    kind: str,
    id: str,
    repo: str = typer.Option(".", "--repo"),
    local: bool = typer.Option(False, "--local"),
    api_url: str | None = typer.Option(None, "--api-url"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect service, client, or endpoint references."""
    _run("refs", repo, local, api_url, json_output, id, kind=kind)


def register(app: typer.Typer) -> None:
    api = typer.Typer(help="Inspect an individual API.")
    api.command("explain")(api_explain)
    app.add_typer(api, name="api")
    app.command("refs")(refs)
