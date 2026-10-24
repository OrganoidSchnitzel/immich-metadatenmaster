"""Background jobs for the web UI. One worker: the model is CPU-heavy and not shared between threads."""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


class JobCancelled(Exception):
    """Raised from the progress callback when the user cancelled the job."""


@dataclass
class Job:
    id: str
    kind: str
    label: str = ""
    # Where the job belongs (e.g. {"album_id": ...}), so a page can pick it up again after a reload.
    meta: dict[str, Any] = field(default_factory=dict)
    cancellable: bool = True
    status: str = "queued"  # queued | running | done | failed | cancelled
    done: int = 0
    total: int = 0
    result: Any = None
    error: str = ""
    cancel_requested: bool = False

    @property
    def active(self) -> bool:
        return self.status in {"queued", "running"}

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "meta": self.meta,
                "cancellable": self.cancellable and self.active, "status": self.status, "done": self.done,
                "total": self.total, "result": self.result, "error": self.error}


@dataclass
class JobManager:
    _jobs: dict[str, Job] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pool: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=1))

    def submit(self, kind: str, fn: Callable[[Callable[[int, int], None]], Any], *, label: str = "",
               meta: dict[str, Any] | None = None, cancellable: bool = True) -> Job:
        """Run `fn(progress)` in the background. A cancelled job stops at its next progress call; the
        functions keep what they did up to then consistent (written changes are logged)."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label or kind, meta=meta or {},
                  cancellable=cancellable)
        with self._lock:
            self._jobs[job.id] = job
            # Keep the most recent jobs only.
            for old in list(self._jobs)[:-50]:
                if not self._jobs[old].active:
                    del self._jobs[old]

        def progress(done: int, total: int) -> None:
            if job.cancel_requested:
                raise JobCancelled()
            job.done, job.total = done, total

        def run() -> None:
            if job.cancel_requested:
                job.status = "cancelled"
                return
            job.status = "running"
            try:
                job.result = fn(progress)
                job.status = "done"
            except JobCancelled:
                job.status, job.error = "cancelled", "Cancelled"
            except Exception as exc:
                log.exception("Job %s (%s) failed", job.id, kind)
                job.error = str(exc) or exc.__class__.__name__
                job.status = "failed"

        self._pool.submit(run)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.active]

    def cancel(self, job_id: str) -> Job | None:
        """Ask a job to stop. Returns None if there is no such job; raises ValueError if it cannot stop."""
        job = self.get(job_id)
        if job is None:
            return None
        if not job.cancellable:
            raise ValueError("This job cannot be cancelled.")
        if job.active:
            job.cancel_requested = True
            if job.status == "queued":
                job.status, job.error = "cancelled", "Cancelled"
        return job

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
