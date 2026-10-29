"""Background index job registry (one running job per server process)."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IndexJob:
    id: str
    repo_path: str
    kind: str = "index"  # index | scan
    status: str = "queued"  # queued | running | done | error | cancelled
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

    def create(self, repo_path: str, kind: str = "index") -> IndexJob:
        job = IndexJob(id=uuid.uuid4().hex[:12], repo_path=repo_path, kind=kind)
        with self.lock:
            self.jobs[job.id] = job
            self.cancel_events[job.id] = threading.Event()
            finished = [j for j in self.jobs.values() if j.status in ("done", "error", "cancelled")]
            for old in finished[:-50]:
                del self.jobs[old.id]
                self.cancel_events.pop(old.id, None)
        return job

    def get(self, job_id: str) -> IndexJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def cancel_event(self, job_id: str) -> threading.Event | None:
        with self.lock:
            return self.cancel_events.get(job_id)

    def update(self, job_id: str, **fields: object) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None:
                for k, v in fields.items():
                    setattr(job, k, v)

    def cancel(self, job_id: str) -> IndexJob | None:
        with self.lock:
            ev = self.cancel_events.get(job_id)
            if ev is not None:
                ev.set()
            return self.jobs.get(job_id)


JOBS = JobRegistry()

# Serializes full index runs: concurrent saves race on repo replacement.
RUN_LOCK = threading.Lock()
