from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class WorkerResult:
    worker_name: str
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    nodes_written: int = 0
    edges_written: int = 0
    nodes_skipped: int = 0  # identical hash → no change needed
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    success: bool = False

    @property
    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def finish(self, success: bool = True) -> WorkerResult:
        self.finished_at = datetime.utcnow()
        self.success = success
        return self


class BaseWorker(ABC):
    """
    Every ingestion worker inherits from this.
    Subclasses implement run() and return a WorkerResult.
    """

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    def run(self) -> WorkerResult: ...
