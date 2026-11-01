from axon.v1.jobs import JobRegistry


def test_newest_queued_index_supersedes_older_same_repo(tmp_path):
    registry = JobRegistry()
    first = registry.create(str(tmp_path))
    latest = registry.create(str(tmp_path / "."))
    assert first.status == "cancelled"
    assert first.result == {"superseded_by": latest.id}
    assert registry.cancel_event(first.id).is_set()
    assert latest.status == "queued"


def test_running_index_and_other_repositories_are_not_cancelled(tmp_path):
    registry = JobRegistry()
    first = registry.create(str(tmp_path / "one"))
    registry.update(first.id, status="running")
    other = registry.create(str(tmp_path / "two"))
    registry.create(str(tmp_path / "one"))
    assert first.status == "running"
    assert other.status == "queued"
    assert registry.index_lock(str(tmp_path / "one")) is registry.index_lock(
        str(tmp_path / "one/.." / "one")
    )
    assert registry.index_lock(str(tmp_path / "one")) is not registry.index_lock(
        str(tmp_path / "two")
    )


def test_two_checkout_paths_for_same_repository_share_queue(tmp_path):
    registry = JobRegistry()
    first = registry.create(str(tmp_path / "one"), repo_id="sample")
    latest = registry.create(str(tmp_path / "two"), repo_id="sample")
    assert first.status == "cancelled"
    assert first.result == {"superseded_by": latest.id}
    assert registry.index_lock(str(tmp_path / "one"), "sample") is registry.index_lock(
        str(tmp_path / "two"), "sample"
    )


def test_jobs_survive_restart_and_running_jobs_are_interrupted(tmp_path):
    registry = JobRegistry(state_dir=tmp_path)
    running = registry.create("/repo")
    registry.update(running.id, status="running", phase="publish", done=2, total=10)
    done = registry.create("/other")
    registry.update(done.id, status="done", result={"repo": "sample"})
    restored = JobRegistry(state_dir=tmp_path)
    assert restored.get(running.id).status == "interrupted"
    assert restored.get(running.id).phase == "publish"
    assert restored.get(done.id).result == {"repo": "sample"}
    assert restored.get(done.id).status == "done"


def test_job_retention_applies_to_disk_and_restart(tmp_path):
    import sqlite3

    registry = JobRegistry(state_dir=tmp_path)
    oldest = registry.create("/repo/oldest", kind="scan")
    registry.update(oldest.id, status="interrupted")
    for index in range(55):
        job = registry.create(f"/repo/{index}", kind="scan")
        registry.update(job.id, status="done")
    assert registry.get(oldest.id) is None
    with sqlite3.connect(tmp_path / "jobs.db") as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 50
    restored = JobRegistry(state_dir=tmp_path)
    assert len(restored.jobs) == 50
    assert restored.get(oldest.id) is None


def test_progress_only_persists_changed_job(tmp_path):
    import sqlite3

    registry = JobRegistry(state_dir=tmp_path)
    first = registry.create("/repo/first", kind="scan")
    second = registry.create("/repo/second", kind="scan")
    with sqlite3.connect(tmp_path / "jobs.db") as db:
        db.executescript(
            "CREATE TABLE writes (id TEXT); "
            "CREATE TRIGGER log_insert AFTER INSERT ON jobs "
            "BEGIN INSERT INTO writes VALUES (new.id); END;"
        )
    registry.update(first.id, phase="parse", done=1)
    with sqlite3.connect(tmp_path / "jobs.db") as db:
        writes = db.execute("SELECT id FROM writes").fetchall()
    assert writes == [(first.id,)]
    assert registry.get(second.id).phase == ""
