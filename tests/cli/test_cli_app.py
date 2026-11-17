from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app as cli_app_module

pytestmark = pytest.mark.tier1

runner = CliRunner()
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "v1" / "monorepo"


def test_index_command_reports_parse_warnings_once(tmp_path) -> None:
    (tmp_path / "axon.yaml").write_text(
        "version: 1\nrepo: r\nservices:\n  - id: svc\n    roots: [svc]\n    language: python\n",
        encoding="utf-8",
    )
    svc = tmp_path / "svc"
    svc.mkdir()
    (svc / "a.py").write_text("PAT = '\\d'\n\ndef f():\n    return 1\n", encoding="utf-8")
    result = runner.invoke(
        cli_app_module.app, ["index", "--repo", str(tmp_path), "--no-cache", "--local"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("parse warnings:") == 1
    assert "<unknown>:" not in result.output


def test_index_command_lists_services() -> None:
    result = runner.invoke(cli_app_module.app, ["index", "--repo", str(FIXTURE), "--local"])
    assert result.exit_code == 0, result.output
    assert "payment" in result.output


def test_init_ai_without_configured_model_fails_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "none")
    from axon.config import get_settings

    get_settings.cache_clear()
    try:
        result = runner.invoke(
            cli_app_module.app, ["init", "--repo", str(tmp_path), "--local", "--ai"]
        )
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1
    assert "LLM_PROVIDER" in result.output


def test_apis_command() -> None:
    result = runner.invoke(cli_app_module.app, ["apis", "--repo", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    assert "/v1/charges" in result.output


def test_ask_command() -> None:
    result = runner.invoke(
        cli_app_module.app,
        ["ask", "What services does POST /checkout call?", "--repo", str(FIXTURE)],
    )
    assert result.exit_code == 0, result.output
    assert "payment" in result.output


def test_watching_interrupted_job_exits_with_failure(monkeypatch):
    class Client:
        def poll_job(self, job_id, on_update):
            return {"status": "interrupted", "error": "server restarted"}

        def close(self):
            pass

    monkeypatch.setattr(cli_app_module, "_client_or_exit", lambda api_url: Client())
    result = runner.invoke(cli_app_module.app, ["jobs", "watch", "restored-job"])
    assert result.exit_code == 1
    assert "server restarted" in result.output


def test_repo_remove_calls_server(monkeypatch):
    import json

    import httpx

    from cli.api_client import ApiClient, ApiConfig

    requests = []

    def serve(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200, json={"repo": "B", "publication_id": "published", "affected_callers": {}}
        )

    client = ApiClient(ApiConfig("http://axon.test"))
    client._client.close()
    client._client = httpx.Client(base_url="http://axon.test", transport=httpx.MockTransport(serve))
    monkeypatch.setattr(cli_app_module, "_client_or_exit", lambda _: client)
    result = runner.invoke(cli_app_module.app, ["repo", "remove", "--repo-id", "B", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["publication_id"] == "published"
    assert requests == [("DELETE", "/api/v1/repos/B")]


def test_local_snapshot_index_preserves_remote_projection(tmp_path, monkeypatch):
    from axon.v1.store import load_snapshot_file
    from tests.v1.test_incremental import remote_repos

    a, b = remote_repos(tmp_path)
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    out = tmp_path / "snapshot.json"
    for root in [b, a]:
        result = runner.invoke(
            cli_app_module.app, ["index", "--repo", str(root), "--out", str(out), "--json"]
        )
        assert result.exit_code == 0, result.output
    snap = load_snapshot_file(out)
    assert snap.coverage_by_service("A")["orders"]["outbound_resolved"] == 1
    assert set(snap.link_inputs) == {"A", "B"}


@pytest.mark.parametrize("snapshot_output", [False, True])
def test_unscoped_local_json_retains_committed_graph_delta(tmp_path, monkeypatch, snapshot_output):
    import json

    from axon.v1.models import EdgeKind
    from axon.v1.store import load_snapshot_file
    from tests.v1.test_incremental import remote_repos

    a, _ = remote_repos(tmp_path)
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    out = tmp_path / "snapshot.json"
    options = ["--out", str(out)] if snapshot_output else ["--local"]
    result = runner.invoke(cli_app_module.app, ["index", "--repo", str(a), *options, "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["graph_delta"]["added"]
    assert report["graph_delta"]["removed"] == []
    assert all(edge["kind"] == EdgeKind.CALLS_API for edge in report["graph_delta"]["added"])
    if snapshot_output:
        snap = load_snapshot_file(out)
        assert report["publication_id"] == snap.publication_id
        assert report["graph_delta"]["added"] == [
            edge.model_dump(mode="json") for edge in snap.edges if edge.kind == EdgeKind.CALLS_API
        ]
        # A second unscoped publication with unchanged links has an empty delta.
        result = runner.invoke(cli_app_module.app, ["index", "--repo", str(a), *options, "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["graph_delta"] == {"added": [], "removed": []}


def test_cli_remove_accepts_slash_repo_identity(tmp_path, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from axon.api.app import create_app
    from cli.api_client import ApiClient, ApiConfig

    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    repo_id = "acme/service"
    (tmp_path / "axon.yaml").write_text("repo: acme/service\nservices: []\n")
    app = create_app()
    with TestClient(app) as server:
        response = server.post("/api/v1/index", json={"repo_path": str(tmp_path)})
        assert response.status_code == 200, response.text
        client = ApiClient(ApiConfig("http://testserver"))
        client._client.close()
        client._client = server
        monkeypatch.setattr(cli_app_module, "_client_or_exit", lambda _: client)
        result = runner.invoke(
            cli_app_module.app, ["repo", "remove", "--repo-id", repo_id, "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["repo"] == repo_id
        assert "repo::" + repo_id not in app.state.v1_store.load().nodes


def test_local_json_delta_uses_its_committed_publication(tmp_path, monkeypatch):
    import json

    import axon.v1.incremental as incremental
    from axon.v1.indexer import index_repo
    from tests.v1.test_incremental import remote_repos

    a, b = remote_repos(tmp_path)
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    publish = incremental.index_and_publish
    publications = []

    def later_writer(root, store, **kwargs):
        result = publish(root, store, **kwargs)
        publications.append(result[0].publication_id)
        target, _ = index_repo(b)
        store.publish_repository(target, repo_id="B")
        publications.append(store.load().publication_id)
        return result

    monkeypatch.setattr(incremental, "index_and_publish", later_writer)
    result = runner.invoke(cli_app_module.app, ["index", "--repo", str(a), "--local", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["publication_id"] == publications[0] != publications[1]
    assert report["coverage"]["orders"]["outbound_external"] == 1
    assert len(report["graph_delta"]["added"]) == 1
    assert report["graph_delta"]["added"][0]["to_id"].startswith("external::")


@pytest.mark.parametrize("command", ["ask", "apis", "pr-impact"])
def test_server_consumers_never_reindex_locally(monkeypatch, command, tmp_path):
    import json

    import httpx

    from cli.api_client import ApiClient, ApiConfig

    calls = []

    def request(req):
        calls.append((req.method, req.url.path, json.loads(req.content) if req.content else {}))
        if req.url.path.endswith("/ask"):
            return httpx.Response(
                200, json={"answer": "B::ledger", "intent": "outbound", "citations": []}
            )
        if req.url.path.endswith("/apis"):
            return httpx.Response(
                200,
                json=[
                    {
                        "repo_id": "B",
                        "service_id": "ledger",
                        "props": {"path": "/run", "method": "GET"},
                        "id": "target",
                    }
                ],
            )
        return httpx.Response(
            200, json={"affected_apis": [], "indexed_commit": "indexed-old", "markdown": "impact"}
        )

    client = ApiClient(ApiConfig("http://server"))
    client._client.close()
    client._client = httpx.Client(base_url="http://server", transport=httpx.MockTransport(request))
    monkeypatch.setattr(cli_app_module, "_client_or_exit", lambda url: client)
    monkeypatch.setattr(
        cli_app_module,
        "index_repo",
        lambda *a, **k: pytest.fail("local indexing is forbidden in server mode"),
    )
    monkeypatch.setattr(cli_app_module, "git_changed_files", lambda *a: ["app/main.py"])
    args = [command] + (["what calls B::ledger?"] if command == "ask" else []) + ["--server"]
    if command == "pr-impact":
        args += ["--repo", str(tmp_path), "--repo-id", "B", "--base", "main"]
    result = runner.invoke(cli_app_module.app, args)
    assert result.exit_code == 0, result.output
    assert (
        calls[-1][1]
        == "/api/v1/" + {"ask": "ask", "apis": "apis", "pr-impact": "impact/graph"}[command]
    )
    if command == "pr-impact":
        assert calls[-1][2]["repo_id"] == "B"


def test_server_impact_reports_stale_selected_commit_without_local_fallback(tmp_path, monkeypatch):
    import json
    import subprocess

    import httpx

    from cli.api_client import ApiClient, ApiConfig

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "test",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    selected = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    def respond(request):
        assert json.loads(request.content)["expected_commit"] == selected
        return httpx.Response(
            409,
            json={
                "detail": {
                    "reason": "indexed-commit-mismatch",
                    "indexed_commit": "older",
                    "expected_commit": selected,
                }
            },
        )

    client = ApiClient(ApiConfig("http://server"))
    client._client.close()
    client._client = httpx.Client(base_url="http://server", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(cli_app_module, "_client_or_exit", lambda _: client)
    monkeypatch.setattr(
        cli_app_module, "index_repo", lambda *a, **k: pytest.fail("no local fallback")
    )
    monkeypatch.setattr(cli_app_module, "git_changed_files", lambda *a: ["app/main.py"])
    result = runner.invoke(
        cli_app_module.app,
        ["pr-impact", "--repo", str(tmp_path), "--base", "HEAD", "--repo-id", "B", "--server"],
    )
    assert result.exit_code == 1 and "indexed-commit-mismatch" in result.output
