"""Background index job registry (one running job per server process)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class IndexJob:
    id: str
    repo_path: str
    repo_id: str = ""
    kind: str = "index"  # index | scan
    status: str = "queued"  # queued | running | done | error | cancelled | interrupted
    phase: str = ""
    done: int = 0
    total: int = 0
    error: str = ""
    result: dict[str, Any] | None = None
    timings: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "repo_path": self.repo_path,
            "repo_id": self.repo_id,
            "status": self.status,
            "phase": self.phase,
            "done": self.done,
            "total": self.total,
            "error": self.error,
            "result": self.result,
            "timings": self.timings,
        }


@dataclass
class JobRegistry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    jobs: dict[str, IndexJob] = field(default_factory=dict)
    cancel_events: dict[str, threading.Event] = field(default_factory=dict)
    index_locks: dict[str, threading.Lock] = field(default_factory=dict)
    state_dir: str | Path | None = None

    def __post_init__(self) -> None:
        if self.state_dir is not None:
            self.configure(self.state_dir)

    def configure(self, state_dir: str | Path) -> None:
        """Restore durable job summaries and mark abandoned jobs interrupted."""
        with self.lock:
            self.state_dir = state_dir
            try:
                path = Path(state_dir) / "jobs.db"
                path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(path) as db:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS jobs "
                        "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                    )
                    for job_id, payload in db.execute("SELECT id, payload FROM jobs"):
                        if job_id in self.jobs:
                            continue
                        job = IndexJob(**json.loads(payload))
                        if job.status in {"queued", "running"}:
                            job.status = "interrupted"
                            job.error = (
                                "server restarted before this job completed; submit a new job"
                            )
                        self.jobs[job_id] = job
                        self.cancel_events[job_id] = threading.Event()
                removed = self._prune_locked()
                self._persist_locked(self.jobs, removed)
            except (OSError, sqlite3.Error, TypeError, ValueError):
                logging.getLogger(__name__).warning("job history unavailable", exc_info=True)

    def _prune_locked(self) -> list[str]:
        finished = [
            j.id
            for j in self.jobs.values()
            if j.status in {"done", "error", "cancelled", "interrupted"}
        ]
        removed = finished[:-50]
        for job_id in removed:
            del self.jobs[job_id]
            self.cancel_events.pop(job_id, None)
        return removed

    def _persist_locked(self, changed: Iterable[str], removed: Iterable[str] = ()) -> None:
        if self.state_dir is None:
            return
        try:
            with sqlite3.connect(Path(self.state_dir) / "jobs.db") as db:
                db.executemany(
                    "INSERT OR REPLACE INTO jobs VALUES (?, ?)",
                    (
                        (job_id, json.dumps(asdict(self.jobs[job_id])))
                        for job_id in changed
                        if job_id in self.jobs
                    ),
                )
                db.executemany("DELETE FROM jobs WHERE id = ?", ((job_id,) for job_id in removed))
        except (OSError, sqlite3.Error, TypeError, ValueError):
            logging.getLogger(__name__).warning("job history not saved", exc_info=True)

    def index_lock(self, repo_path: str, repo_id: str = "") -> threading.Lock:
        key = "repo:" + repo_id if repo_id else "path:" + str(Path(repo_path).resolve())
        with self.lock:
            return self.index_locks.setdefault(key, threading.Lock())

    def create(self, repo_path: str, kind: str = "index", repo_id: str = "") -> IndexJob:
        job = IndexJob(id=uuid.uuid4().hex[:12], repo_path=repo_path, repo_id=repo_id, kind=kind)
        with self.lock:
            changed = [job.id]
            if kind == "index":
                for old in self.jobs.values():
                    if (
                        old.kind == "index"
                        and old.status == "queued"
                        and (
                            (repo_id and old.repo_id == repo_id)
                            or (
                                not repo_id
                                and Path(old.repo_path).resolve() == Path(repo_path).resolve()
                            )
                        )
                    ):
                        old.status = "cancelled"
                        old.result = {"superseded_by": job.id}
                        self.cancel_events[old.id].set()
                        changed.append(old.id)
            self.jobs[job.id] = job
            self.cancel_events[job.id] = threading.Event()
            removed = self._prune_locked()
            self._persist_locked(changed, removed)
        return job

    def get(self, job_id: str) -> IndexJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def snapshot(self, job_id: str, *, cancel: bool = False) -> dict[str, Any] | None:
        """Read a consistent response; callers offload lock acquisition from ASGI."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if cancel:
                self.cancel_events[job_id].set()
            return job.to_dict()

    def cancel_event(self, job_id: str) -> threading.Event | None:
        with self.lock:
            return self.cancel_events.get(job_id)

    def update(self, job_id: str, **fields: object) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None:
                for k, v in fields.items():
                    setattr(job, k, v)
                removed = self._prune_locked()
                self._persist_locked([job_id], removed)

    def cancel(self, job_id: str) -> IndexJob | None:
        with self.lock:
            ev = self.cancel_events.get(job_id)
            if ev is not None:
                ev.set()
            return self.jobs.get(job_id)


JOBS = JobRegistry()

# Serializes full index runs: concurrent saves race on repo replacement.
RUN_LOCK = threading.Lock()
