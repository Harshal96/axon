from axon.v2.jobs import JobRegistry


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
