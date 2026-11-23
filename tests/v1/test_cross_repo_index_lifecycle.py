import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from axon.api.app import create_app
from axon.v1 import api
from axon.v1.jobs import JobRegistry
from axon.v1.store import MemoryStore
from tests.v1.test_incremental import commit, git, remote_repos

pytestmark = pytest.mark.tier1


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXON_FACTS_CACHE_DIR", str(tmp_path / "facts"))
    monkeypatch.setattr(api, "JOBS", JobRegistry())
    app = create_app()
    app.state.v1_store = MemoryStore()
    with TestClient(app, raise_server_exceptions=False) as connection:
        yield connection


def publish(client, root, **kwargs):
    response = client.post("/api/v1/index", json={"repo_path": str(root), **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def wait_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get("/api/v1/index/jobs/" + job_id).json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail(f"index job did not complete: {job}")


@pytest.mark.parametrize("endpoint", ["/index", "/index/jobs"])
@pytest.mark.parametrize("previously_published", [False, True])
def test_outdated_revision_rejected_before_skip_or_publication(
    tmp_path, client, endpoint, previously_published
):
    a, b = remote_repos(tmp_path)
    old = git(b, "rev-parse", "HEAD")
    if previously_published:
        publish(client, b)
    # Checked-out routes now belong to a different revision.
    source = b / "ledger/api.py"
    source.write_text(source.read_text().replace("/entries", "/replacement"))
    new = commit(b)
    store = client.app.state.v1_store
    prior = store.load()
    response = client.post("/api/v1" + endpoint, json={"repo_path": str(b), "commit": old})
    assert response.status_code == 400, response.text
    assert "checkout HEAD" in response.json()["detail"]
    assert old in response.json()["detail"]
    assert new in response.json()["detail"]
    assert store.load() is prior
    if previously_published:
        current = publish(client, a)
        assert current["coverage"]["orders"]["outbound_resolved"] == 1


def test_dirty_checkout_can_publish_against_actual_head(tmp_path, client):
    _, b = remote_repos(tmp_path)
    head = git(b, "rev-parse", "HEAD")
    source = b / "ledger/api.py"
    source.write_text(source.read_text().replace("/entries", "/replacement"))
    publish(client, b, commit=head[:10])
    repo = client.get("/api/v1/repos").json()[0]
    assert repo["props"]["commit"] == head
    assert repo["props"]["dirty"] == "true"
    assert client.get("/api/v1/apis", params={"repo": "B"}).json()[0]["label"] == "GET /replacement"


@pytest.mark.parametrize("after_validation", [False, True])
def test_head_change_after_assembly_cannot_publish(tmp_path, client, monkeypatch, after_validation):
    _, b = remote_repos(tmp_path)
    publish(client, b)
    store = client.app.state.v1_store
    before = store.load()
    publish_repository = store.publish_repository

    def move_checkout():
        (b / "ledger" / "new_module.py").write_text("def helper(): pass\n")
        commit(b)

    def move_checkout_then_publish(replacement, *, repo_id, validate):
        def staged(candidate):
            if not after_validation:
                move_checkout()
            validate(candidate)
            if after_validation:
                # Neo4j reserves one progress step after its last staging batch
                # for the atomic activation of the publication.
                move_checkout()
                store.publish_progress(1, 2)

        return publish_repository(replacement, repo_id=repo_id, validate=staged)

    monkeypatch.setattr(store, "publish_progress", None, raising=False)
    monkeypatch.setattr(store, "publish_repository", move_checkout_then_publish)
    response = client.post("/api/v1/index", json={"repo_path": str(b), "force": True})
    assert response.status_code == 409, response.text
    assert "checkout/configuration changed" in response.json()["detail"]
    assert store.load() is before


def test_checkout_cannot_keep_publishing_under_its_old_identity(tmp_path, client):
    a, b = remote_repos(tmp_path)
    publish(client, b)
    publish(client, a)
    config = b / "axon.yaml"
    config.write_text(config.read_text().replace("repo: B", "repo: newB"))
    for endpoint in ["/index", "/index/jobs"]:
        response = client.post(
            "/api/v1" + endpoint, json={"repo_path": str(b), "replace_source": True}
        )
        assert response.status_code == 409, response.text
        assert "remove" in response.json()["detail"].lower()
    assert client.delete("/api/v1/repos/B").status_code == 200
    publish(client, b)
    assert {row["repo_id"] for row in client.get("/api/v1/repos").json()} == {"A", "newB"}
    result = publish(client, a)
    assert result["coverage"]["orders"]["outbound_external"] == 1


@pytest.fixture
def worker_dispatch(monkeypatch):
    dispatched = threading.Event()
    release = threading.Event()
    original = api.JOBS.index_lock

    def delay_worker(repo_path, repo_id=""):
        if repo_id == "B" and repo_path:
            dispatched.set()
            assert release.wait(5)
        return original(repo_path, repo_id)

    def hold():
        monkeypatch.setattr(api.JOBS, "index_lock", delay_worker)
        return dispatched, release

    try:
        yield hold
    finally:
        release.set()


def test_removal_cancels_accepted_but_undispatched_index_jobs(tmp_path, client, worker_dispatch):
    a, b = remote_repos(tmp_path)
    publish(client, b)
    publish(client, a)
    dispatched, release = worker_dispatch()
    submitted = client.post("/api/v1/index/jobs", json={"repo_path": str(b)})
    assert submitted.status_code == 202, submitted.text
    assert dispatched.wait(5)
    removed = client.delete("/api/v1/repos/B")
    assert removed.status_code == 200, removed.text
    release.set()
    job = wait_job(client, submitted.json()["job_id"])
    assert job["status"] == "cancelled", job
    assert {row["repo_id"] for row in client.get("/api/v1/repos").json()} == {"A"}
    assert publish(client, a)["coverage"]["orders"]["outbound_external"] == 1
    publish(client, b)  # A new submission after removal may restore B.
    assert publish(client, a)["coverage"]["orders"]["outbound_resolved"] == 1


def test_queued_revision_request_stays_pinned_to_accepted_revision(
    tmp_path, client, worker_dispatch
):
    _, b = remote_repos(tmp_path)
    dispatched, release = worker_dispatch()
    submitted = client.post("/api/v1/index/jobs", json={"repo_path": str(b), "commit": "HEAD"})
    assert submitted.status_code == 202, submitted.text
    assert dispatched.wait(5)
    (b / "ledger" / "new_module.py").write_text("def helper(): pass\n")
    new = commit(b)
    release.set()
    job = wait_job(client, submitted.json()["job_id"])
    assert job["status"] == "error", job
    assert new in job["error"]
    assert client.get("/api/v1/repos").json() == []


def test_index_job_validation_does_not_block_health(tmp_path, client, monkeypatch):
    a, _ = remote_repos(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    store = client.app.state.v1_store
    load = store.load

    def delayed_load():
        entered.set()
        assert release.wait(8)
        return load()

    with ThreadPoolExecutor() as pool, monkeypatch.context() as patch:
        patch.setattr(store, "load", delayed_load)
        submission = pool.submit(client.post, "/api/v1/index/jobs", json={"repo_path": str(a)})
        try:
            assert entered.wait(5)
            health = pool.submit(client.get, "/health")
            assert health.result(timeout=2).status_code == 200
        finally:
            release.set()
        submitted = submission.result(timeout=5)
    assert submitted.status_code == 202, submitted.text
    assert wait_job(client, submitted.json()["job_id"])["status"] == "done"


def test_index_job_reports_missing_service_map(tmp_path, client):
    response = client.post("/api/v1/index/jobs", json={"repo_path": str(tmp_path)})
    assert response.status_code == 400, response.text
    assert "axon.yaml" in response.json()["detail"]
