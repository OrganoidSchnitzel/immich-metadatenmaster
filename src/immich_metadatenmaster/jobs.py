"""Background jobs for the web UI. One worker: the model is CPU-heavy and not shared between threads."""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued | running | done | failed
    done: int = 0
    total: int = 0
    result: Any = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "status": self.status, "done": self.done,
                "total": self.total, "result": self.result, "error": self.error}


@dataclass
class JobManager:
    _jobs: dict[str, Job] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pool: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=1))

    def submit(self, kind: str, fn: Callable[[Callable[[int, int], None]], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job
            # Keep the most recent jobs only.
            for old in list(self._jobs)[:-50]:
                if self._jobs[old].status in {"done", "failed"}:
                    del self._jobs[old]

        def progress(done: int, total: int) -> None:
            job.done, job.total = done, total

        def run() -> None:
            job.status = "running"
            try:
                job.result = fn(progress)
                job.status = "done"
            except Exception as exc:
                log.exception("Job %s (%s) failed", job.id, kind)
                job.error = str(exc) or exc.__class__.__name__
                job.status = "failed"

        self._pool.submit(run)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
