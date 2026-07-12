"""Durable long-running job registry with cooperative cancellation.

The public API intentionally remains small because the HTTP routers use this
module from worker threads:

    with project_job(project_id, "Export", resource="gpu") as cancel_event:
        ...

Only one job may mutate a project at a time.  Supplying ``resource`` also
serializes jobs that use a process-wide scarce resource (for example ``gpu``
or ``ffmpeg``), even when they belong to different projects.

Job metadata is written to SQLite so completed/failed jobs survive a server
restart.  A row left in ``running`` state when this module initializes is
marked ``interrupted``.  Cancellation remains cooperative: long operations
must call :func:`check` (directly or through :func:`checking_logger`).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


JOBS_DB_ENV = "PIPELINE_JOBS_DB"
_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB_PATH = _WORKSPACE_ROOT / "projects" / ".pipeline_jobs.sqlite3"


class ProjectBusy(Exception):
    """Another job is already running for this project or resource."""


class JobCancelled(Exception):
    """The user requested cancellation."""


_meta = threading.RLock()
_db_lock = threading.RLock()
_locks: dict[str, threading.Lock] = {}
_resource_locks: dict[str, threading.Lock] = {}
_settings_lock = threading.Lock()
_events: dict[str, threading.Event] = {}
_running: dict[str, str] = {}  # project_id -> human-readable job name
_resource_running: dict[str, tuple[str, str]] = {}  # resource -> (project_id, name)
_event_jobs: dict[threading.Event, str] = {}  # active cancel event -> durable job id
_initialized_db_path: Path | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _database_path() -> Path:
    configured = os.environ.get(JOBS_DB_ENV, "").strip()
    if not configured:
        return _DEFAULT_DB_PATH
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = _WORKSPACE_ROOT / path
    return path.resolve()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _ensure_database() -> Path:
    """Create/migrate the database and recover jobs abandoned by a restart."""
    global _initialized_db_path

    path = _database_path()
    with _db_lock:
        if _initialized_db_path == path:
            return path

        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(_connect(path)) as connection:
            # WAL lets a future status/history endpoint read while a worker is
            # appending progress.  All writes in this module are also guarded
            # by _db_lock, so connections are never shared between threads.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    resource TEXT,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0.0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_project_created "
                "ON jobs(project_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_updated "
                "ON jobs(status, updated_at DESC)"
            )

            now = _utc_now()
            connection.execute(
                """
                UPDATE jobs
                   SET status = 'interrupted',
                       message = CASE
                           WHEN message = '' THEN 'Interrupted by server restart'
                           ELSE message
                       END,
                       error = CASE
                           WHEN error = '' THEN 'Server stopped before the job completed'
                           ELSE error
                       END,
                       updated_at = ?,
                       finished_at = COALESCE(finished_at, ?)
                 WHERE status = 'running'
                """,
                (now, now),
            )
            connection.commit()

        _initialized_db_path = path
        return path


def _execute(sql: str, parameters: tuple[Any, ...] = ()) -> None:
    path = _ensure_database()
    with _db_lock, closing(_connect(path)) as connection:
        connection.execute(sql, parameters)
        connection.commit()


def _query_one(sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    path = _ensure_database()
    with _db_lock, closing(_connect(path)) as connection:
        row = connection.execute(sql, parameters).fetchone()
        return dict(row) if row is not None else None


def _query_all(sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    path = _ensure_database()
    with _db_lock, closing(_connect(path)) as connection:
        rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]


def _lock_for(project_id: str) -> threading.Lock:
    with _meta:
        return _locks.setdefault(project_id, threading.Lock())


def _resource_lock_for(resource: str) -> threading.Lock:
    with _meta:
        return _resource_locks.setdefault(resource, threading.Lock())


def _event_for(project_id: str) -> threading.Event:
    with _meta:
        return _events.setdefault(project_id, threading.Event())


def _start_record(project_id: str, name: str, resource: str | None) -> str:
    job_id = uuid.uuid4().hex
    now = _utc_now()
    _execute(
        """
        INSERT INTO jobs (
            id, project_id, name, resource, status, progress, message, error,
            created_at, started_at, updated_at, finished_at
        ) VALUES (?, ?, ?, ?, 'running', 0.0, '', '', ?, ?, ?, NULL)
        """,
        (job_id, project_id, name, resource, now, now, now),
    )
    return job_id


def _finish_record(job_id: str, status: str, error: str = "") -> None:
    now = _utc_now()
    progress = 1.0 if status == "succeeded" else None
    default_message = {
        "succeeded": "Completed",
        "failed": "Failed",
        "cancelled": "Cancelled",
    }.get(status, status.capitalize())
    _execute(
        """
        UPDATE jobs
           SET status = ?,
               progress = COALESCE(?, progress),
               message = CASE WHEN message = '' THEN ? ELSE message END,
               error = ?,
               updated_at = ?,
               finished_at = ?
         WHERE id = ?
        """,
        (status, progress, default_message, error, now, now, job_id),
    )


def _job_id_for_event(event: threading.Event | None) -> str:
    if event is None:
        return ""
    with _meta:
        return _event_jobs.get(event, "")


class project_job:
    """Guard one long job per project and, optionally, per resource.

    ``resource`` is deliberately optional so all existing call sites retain
    their behaviour.  Resource names are normalized to lower case.
    """

    def __init__(self, project_id: str, name: str, resource: str | None = None):
        self.project_id = str(project_id)
        self.name = str(name)
        self.resource = resource.strip().lower() if resource and resource.strip() else None
        self.job_id = ""
        self._lock = _lock_for(self.project_id)
        self._resource_lock = _resource_lock_for(self.resource) if self.resource else None
        self._event: threading.Event | None = None

    def __enter__(self) -> threading.Event:
        if not self._lock.acquire(blocking=False):
            with _meta:
                running = _running.get(self.project_id, "другая задача")
            raise ProjectBusy(
                f"Проект занят: выполняется «{running}». Дождитесь завершения или отмените её."
            )

        resource_acquired = False
        try:
            if self._resource_lock is not None:
                resource_acquired = self._resource_lock.acquire(blocking=False)
                if not resource_acquired:
                    with _meta:
                        owner = _resource_running.get(self.resource or "")
                    running = owner[1] if owner else "другая задача"
                    raise ProjectBusy(
                        f"Ресурс «{self.resource}» занят: выполняется «{running}». "
                        "Дождитесь завершения или отмените её."
                    )

            event = _event_for(self.project_id)
            event.clear()
            self.job_id = _start_record(self.project_id, self.name, self.resource)
            self._event = event
            with _meta:
                _running[self.project_id] = self.name
                _event_jobs[event] = self.job_id
                if self.resource:
                    _resource_running[self.resource] = (self.project_id, self.name)
            return event
        except BaseException:
            if resource_acquired and self._resource_lock is not None:
                self._resource_lock.release()
            self._lock.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            status = "succeeded"
            error = ""
        elif issubclass(exc_type, JobCancelled):
            status = "cancelled"
            error = str(exc or "Задача отменена пользователем")
        else:
            status = "failed"
            error = f"{exc_type.__name__}: {exc}" if exc is not None else exc_type.__name__

        persistence_error: BaseException | None = None
        try:
            if self.job_id:
                _finish_record(self.job_id, status, error)
        except BaseException as caught:
            # Never hide the exception raised by the actual pipeline job.
            persistence_error = caught
        finally:
            with _meta:
                _running.pop(self.project_id, None)
                if self._event is not None:
                    _event_jobs.pop(self._event, None)
                if self.resource:
                    _resource_running.pop(self.resource, None)
            if self._resource_lock is not None:
                self._resource_lock.release()
            self._lock.release()

        if persistence_error is not None and exc_type is None:
            raise persistence_error
        return False


class project_mutation:
    """Serialize a short project write without creating a durable job row.

    HTTP patch/save handlers use the same per-project lock as long-running
    workers.  A separate ``running_job()`` check has a time-of-check/time-of-use
    gap: a render can acquire the lock after the check but before the board is
    written.  This guard closes that gap while keeping routine UI edits out of
    the durable job history.

    Pipeline workers that already hold :class:`project_job` continue to write
    their artifacts directly; they must not enter this non-reentrant guard.
    """

    def __init__(self, project_id: str, name: str = "Project update"):
        self.project_id = str(project_id)
        self.name = str(name)
        self._lock = _lock_for(self.project_id)
        self._entered = False

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            with _meta:
                running = _running.get(self.project_id, "другая задача")
            raise ProjectBusy(
                f"Проект занят: выполняется «{running}». Дождитесь завершения или отмените её."
            )
        self._entered = True
        with _meta:
            _running[self.project_id] = self.name
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._entered:
            return False
        try:
            with _meta:
                if _running.get(self.project_id) == self.name:
                    _running.pop(self.project_id, None)
        finally:
            self._entered = False
            self._lock.release()
        return False


class settings_mutation:
    """Serialize global settings updates without blocking the request thread.

    Settings writes are read-modify-write operations (most notably, blank
    secret fields preserve the existing value).  A dedicated lock prevents
    two concurrent PUT requests from validating against the same stale
    snapshot and silently losing one update.  Project jobs deliberately do
    not use this lock; their interaction with settings updates is coordinated
    by :class:`bulk_project_mutation` below.
    """

    def __init__(self, name: str = "Global settings update"):
        self.name = str(name)
        self._entered = False

    def __enter__(self):
        if not _settings_lock.acquire(blocking=False):
            raise ProjectBusy(
                "Настройки заняты: уже выполняется другое обновление. Повторите запрос."
            )
        self._entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._entered:
            self._entered = False
            _settings_lock.release()
        return False


class bulk_project_mutation:
    """Atomically acquire the mutation locks for a set of projects.

    Locks are attempted in a stable order and never waited on.  If any one is
    busy, every lock acquired earlier in the attempt is released before
    :class:`ProjectBusy` is raised.  Consequently a caller can safely defer
    all filesystem changes until after entering this context: the operation
    either owns every listed project or owns none of them.

    This guard does not create durable job rows and is intentionally separate
    from :class:`settings_mutation`; the latter serializes the global
    read-modify-write while this class coordinates it with project workers.
    """

    def __init__(self, project_ids: list[str] | tuple[str, ...] | set[str], name: str):
        self.project_ids = sorted({str(project_id) for project_id in project_ids})
        self.name = str(name)
        self._acquired: list[tuple[str, threading.Lock]] = []
        self._entered = False

    def __enter__(self):
        for project_id in self.project_ids:
            lock = _lock_for(project_id)
            if lock.acquire(blocking=False):
                self._acquired.append((project_id, lock))
                continue

            with _meta:
                running = _running.get(project_id, "другая задача")
            self._release_acquired()
            raise ProjectBusy(
                f"Проект занят: выполняется «{running}». Дождитесь завершения или отмените её."
            )

        self._entered = True
        with _meta:
            for project_id, _lock in self._acquired:
                _running[project_id] = self.name
        return self

    def _release_acquired(self) -> None:
        for _project_id, lock in reversed(self._acquired):
            lock.release()
        self._acquired.clear()

    def __exit__(self, exc_type, exc, tb):
        if not self._entered:
            return False
        try:
            with _meta:
                for project_id, _lock in self._acquired:
                    if _running.get(project_id) == self.name:
                        _running.pop(project_id, None)
        finally:
            self._entered = False
            self._release_acquired()
        return False


def request_cancel(project_id: str) -> str:
    """Signal the running job (if any) to stop. Returns the job name or ``''``."""
    with _meta:
        name = _running.get(project_id, "")
        event = _events.setdefault(project_id, threading.Event())
        job_id = _event_jobs.get(event, "")
        event.set()
    if job_id:
        _execute(
            "UPDATE jobs SET message = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            ("Cancellation requested", _utc_now(), job_id),
        )
    return name


def running_job(project_id: str) -> str:
    with _meta:
        return _running.get(project_id, "")


def check(event: threading.Event | None) -> None:
    """Raise :class:`JobCancelled` if cancellation was requested."""
    if event is not None and event.is_set():
        raise JobCancelled("Задача отменена пользователем")


def update_progress(
    event: threading.Event | None,
    progress: float | None = None,
    message: str | None = None,
) -> None:
    """Persist progress for the active job associated with ``event``.

    Progress is a fraction in the inclusive range 0..1.  The helper is safe to
    call with ``None`` or an event that no longer belongs to an active job.
    """
    job_id = _job_id_for_event(event)
    if not job_id:
        return
    if progress is not None:
        progress = float(progress)
        if not 0.0 <= progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")
    if progress is None and message is None:
        return

    now = _utc_now()
    if progress is None:
        _execute(
            "UPDATE jobs SET message = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            (str(message), now, job_id),
        )
    elif message is None:
        _execute(
            "UPDATE jobs SET progress = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            (progress, now, job_id),
        )
    else:
        _execute(
            """
            UPDATE jobs SET progress = ?, message = ?, updated_at = ?
             WHERE id = ? AND status = 'running'
            """,
            (progress, str(message), now, job_id),
        )


def checking_logger(log, event: threading.Event | None):
    """Wrap a logger so each line is a cancel checkpoint and durable message."""

    last_persisted = 0.0

    def wrapped(msg: str):
        nonlocal last_persisted
        check(event)
        # ffmpeg may emit many lines per second. Persist a representative latest
        # message without turning SQLite into a per-frame logging bottleneck.
        now = time.monotonic()
        if now - last_persisted >= 0.25:
            update_progress(event, message=msg)
            last_persisted = now
        if log:
            log(msg)

    return wrapped


def job_record(job_id: str) -> dict[str, Any] | None:
    """Return one durable job record, primarily for status APIs and diagnostics."""
    return _query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))


def job_history(project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Return newest durable jobs, optionally restricted to one project."""
    limit = max(1, min(int(limit), 1000))
    if project_id is None:
        return _query_all(
            "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    return _query_all(
        "SELECT * FROM jobs WHERE project_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (project_id, limit),
    )
