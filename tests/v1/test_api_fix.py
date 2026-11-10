import time
from pathlib import Path

from fastapi.testclient import TestClient

from axon.api.app import create_app
from axon.v1.indexer import index_repo
from axon.v1.models import NodeKind
from axon.v1.scoped_index import endpoint_files, focused_api_result, resolve_endpoint

APP = (
    "from fastapi import FastAPI\n"
    "app = FastAPI()\n"
    "@app.get('/items')\n"
    "def list_items():\n    return []\n"
    "def replacement():\n    return []\n"
)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text(APP)
    (tmp_path / "axon.yaml").write_text("repo: demo\nservices:\n- {id: app, roots: [app]}\n")
    return tmp_path


def _endpoint_id(client: TestClient) -> str:
    apis = client.get("/api/v1/apis?repo=demo").json()
    assert len(apis) == 1
    return apis[0]["id"]


def _wait_job(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        job = client.get(f"/api/v1/index/jobs/{job_id}").json()
        if job["status"] in {"done", "error", "cancelled", "interrupted"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_endpoint_files_and_focused_result(tmp_path):
    root = _repo(tmp_path)
    snap, _ = index_repo(root, workers=1, use_cache=False)
    ep = snap.by_kind(NodeKind.API_ENDPOINT)[0]
    assert endpoint_files(snap, ep.id) == {"app/main.py"}
    assert resolve_endpoint(snap, "GET /items").id == ep.id
    report = focused_api_result(snap, snap, ep.id)
    assert report["changed"] is False
    assert report["graph_delta"] == {"added": [], "removed": []}
    assert report["handlers"][0]["name"] == "list_items"


def test_recheck_job_reports_no_change(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setenv("AXON_FACTS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AXON_INDEX_WORKERS", "1")
    with TestClient(create_app()) as client:
        assert client.post("/api/v1/index", json={"repo_path": str(root)}).status_code == 200
        ep_id = _endpoint_id(client)
        resp = client.post(f"/api/v1/apis/{ep_id}/recheck")
        assert resp.status_code == 202, resp.text
        job = _wait_job(client, resp.json()["job_id"])
        assert job["status"] == "done", job
        assert job["result"]["api"] == ep_id
        assert job["result"]["changed"] is False


def test_correction_preview_apply_and_survives_reindex(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setenv("AXON_FACTS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AXON_INDEX_WORKERS", "1")
    before_yaml = (root / "axon.yaml").read_text()
    with TestClient(create_app()) as client:
        assert client.post("/api/v1/index", json={"repo_path": str(root)}).status_code == 200
        ep_id = _endpoint_id(client)
        body = {"handler": "replacement"}
        preview = client.post(f"/api/v1/apis/{ep_id}/corrections/preview", json=body)
        assert preview.status_code == 200, preview.text
        assert "handlers:" in str(preview.json()["diffs"])
        assert (root / "axon.yaml").read_text() == before_yaml  # preview writes nothing

        applied = client.post(f"/api/v1/apis/{ep_id}/corrections/apply", json=body)
        assert applied.status_code == 200, applied.text
        assert applied.json()["applied"] is True
        assert "replacement" in (root / "axon.yaml").read_text()
        job = _wait_job(client, applied.json()["job_id"])
        assert job["status"] == "done", job
        detail = client.get(f"/api/v1/apis/{ep_id}").json()
        assert detail["handler"]["name"] == "replacement"

    snap, _ = index_repo(root, workers=1, use_cache=False)
    ep = snap.by_kind(NodeKind.API_ENDPOINT)[0]
    assert snap.handler_of(ep.id).name == "replacement"


def test_correction_rejects_bad_handler_evidence_and_prompt(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setenv("AXON_FACTS_CACHE_DIR", str(tmp_path / "cache"))
    with TestClient(create_app()) as client:
        assert client.post("/api/v1/index", json={"repo_path": str(root)}).status_code == 200
        ep_id = _endpoint_id(client)
        bad_handler = client.post(
            f"/api/v1/apis/{ep_id}/corrections/preview", json={"handler": "missing_fn"}
        )
        assert bad_handler.status_code == 400
        bad_evidence = client.post(
            f"/api/v1/apis/{ep_id}/corrections/preview",
            json={
                "add_calls": [
                    {
                        "to_service": "fraud",
                        "to_method": "POST",
                        "to_path": "/score",
                        "evidence_file": "app/nope.py",
                        "evidence_line": 1,
                        "evidence_text": "fraud.score(req)",
                    }
                ]
            },
        )
        assert bad_evidence.status_code == 400
        prompt = client.post(
            f"/api/v1/apis/{ep_id}/corrections/preview",
            json={"prompt": "handled by replacement"},
        )
        assert prompt.status_code == 400  # no LLM configured in tests
        assert "LLM" in prompt.json()["detail"]


def test_prompt_draft_drops_unresolvable_parts(tmp_path):
    import asyncio
    import json

    from axon.v1.api_fix import draft_from_prompt

    root = _repo(tmp_path)
    snap, _ = index_repo(root, workers=1, use_cache=False)
    ep = snap.by_kind(NodeKind.API_ENDPOINT)[0]

    class FakeLlm:
        async def chat(self, system, user):
            return json.dumps(
                {
                    "handler": "replacement",
                    "add_calls": [
                        {
                            "to_service": "ghost",
                            "to_method": "GET",
                            "to_path": "/nope",
                            "evidence_file": "app/main.py",
                            "evidence_line": 1,
                            "evidence_text": "x",
                        }
                    ],
                    "suppress": [{"to_service": "", "reason": "dead"}],
                }
            )

    draft, warnings = asyncio.run(
        draft_from_prompt(FakeLlm(), snap, ep.id, "handled by replacement")
    )
    assert draft.handler == "replacement"
    assert draft.add_calls == []  # unindexed target dropped, not invented
    assert draft.suppress[0].reason == "dead"
    assert any("dropped call draft" in w for w in warnings)


def test_cli_api_rejects_local_mode(tmp_path):
    from typer.testing import CliRunner

    from cli.app import app

    _repo(tmp_path)
    result = CliRunner().invoke(
        app, ["index", "--repo", str(tmp_path), "--local", "--api", "GET /items"]
    )
    assert result.exit_code == 2
    assert "--api needs API server mode" in result.output


def test_recheck_unknown_api_404(tmp_path):
    _repo(tmp_path)
    with TestClient(create_app()) as client:
        resp = client.post("/api/v1/apis/endpoint::demo::app::GET:/nope/recheck")
        assert resp.status_code == 404
