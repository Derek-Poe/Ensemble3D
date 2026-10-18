"""Bounded, single-owner local scheduling; job state is durable in SQLite.

This maintains the original ThreadPoolExecutor design while adding admission
control, shutdown, process ownership and atomic version/job completion.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading

from .db import StudioDB, new_id


class QueueFull(RuntimeError):
    """All running and waiting slots are occupied; nothing was admitted."""


class WorkspaceBusy(RuntimeError):
    """Another manager already owns this local database."""


class WorkspaceLease:
    """OS file lock released automatically on process exit, including a crash."""

    def __init__(self, path: Path):
        self.fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b"0")
            os.lseek(self.fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise WorkspaceBusy("This workspace already has an active manager") from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class JobManager:
    def __init__(self, db: StudioDB, max_workers: int = 2, max_pending: int = 4):
        if (type(max_workers) is not int or type(max_pending) is not int
                or not 1 <= max_workers <= 32 or not 0 <= max_pending <= 1024):
            raise ValueError("workers must be 1..32 and pending slots 0..1024")
        self.db = db
        self.max_workers = max_workers
        self.capacity = max_workers + max_pending
        self._lease = WorkspaceLease(db.db_path.with_suffix(".owner.lock"))
        try:
            self.recovered_jobs = db.recover_interrupted_jobs()
            self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ensemble3d")
        except BaseException:
            self._lease.close()
            raise
        self._lock = threading.RLock()
        self._permits = threading.BoundedSemaphore(self.capacity)
        self._futures = set()
        self._closed = False

    def submit(self, *, project_id: str, kind: str, payload: dict, run, job_id=None) -> str:
        """Admit immediately or reject; run returns a version draft or None.

        Callables are process-local. Only their kind/payload/state is persisted;
        interrupted work is marked error on restart, never silently replayed.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("Job manager is closed")
            if not self._permits.acquire(blocking=False):
                raise QueueFull("Local job capacity reached; retry after a job finishes")
            jid = job_id or new_id("job")
            created = False
            try:
                self.db.create_job(job_id=jid, project_id=project_id, kind=kind, payload=payload)
                created = True
                future = self._executor.submit(self._run, jid, run)
            except BaseException:
                try:
                    if created:
                        self.db.update_job(jid, status="error", stage="admission_failed", message="Job was not started")
                finally:
                    self._permits.release()
                raise
            self._futures.add(future)
            future.add_done_callback(lambda done: self._finished(jid, done))
            return jid

    def _run(self, jid, run):
        try:
            self.db.update_job(jid, status="running", stage="working", message="Running")
            draft = run(jid)
            if draft is None:
                self.db.update_job(jid, status="done", progress=1, stage="completed", message="Completed")
            else:
                # One transaction links the new version/current pointer and
                # successful job. A crash cannot leave just half of that commit.
                self.db.create_model_version(**draft, job_id=jid)
        except BaseException as error:
            # Keep engine paths, command lines and environment values out of the
            # public job response. No arbitrary engine is shipped in this slice.
            self.db.update_job(jid, status="error", stage="failed", message="Job failed",
                               error=type(error).__name__)

    def _finished(self, jid, future):
        try:
            if future.cancelled():
                self.db.update_job(jid, status="error", stage="cancelled", message="Cancelled before starting")
        finally:
            with self._lock:
                self._futures.discard(future)
                self._permits.release()

    def get(self, job_id):
        return self.db.get_job(job_id)

    def close(self, *, cancel_pending=False):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            # Running Python threads are not force-killed. Shipped synthetic
            # jobs are finite; callers supplying handlers must arrange a bound.
            self._executor.shutdown(wait=True, cancel_futures=cancel_pending)
        finally:
            self._lease.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
