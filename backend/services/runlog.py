"""Bounded, structured per-project run logs.

The durable jobs registry stores job metadata (status, timing, last message).
This module stores the diagnostic message stream next to the project so a run
can be investigated after its SSE client has disconnected.

Files live in ``<project>/logs``.  The first file of a day retains the legacy
``runs-YYYYMMDD.jsonl`` name; size rotation adds ``-001``, ``-002``, ... .
Every object retains the original ``ts`` / ``job`` / ``msg`` fields and adds a
small structured envelope::

    {"v": 1, "ts": "...Z", "job": "Export", "msg": "...",
     "run_id": "...", "job_id": "...", "event": "progress",
     "level": "info", "fields": {"segment": 4}}

Retention is deliberately bounded.  Defaults keep 14 files of at most 5 MiB
per project and can be changed with ``PIPELINE_RUNLOG_MAX_FILES`` and
``PIPELINE_RUNLOG_MAX_BYTES``.  Messages are capped separately by
``PIPELINE_RUNLOG_MAX_MESSAGE_CHARS`` (16 KiB by default).

Failure policy is strict: run logging must never break a pipeline job.  Every
filesystem, serialization, rotation and retention error is swallowed.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


RUNLOG_MAX_FILES_ENV = "PIPELINE_RUNLOG_MAX_FILES"
RUNLOG_MAX_BYTES_ENV = "PIPELINE_RUNLOG_MAX_BYTES"
RUNLOG_MAX_MESSAGE_CHARS_ENV = "PIPELINE_RUNLOG_MAX_MESSAGE_CHARS"

DEFAULT_MAX_FILES = 14
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_MESSAGE_CHARS = 16 * 1024
MAX_READ_RECORDS = 500

_write_lock = threading.Lock()
_active_paths: dict[tuple[Path, str, int], Path] = {}
_known_logs_dirs: set[Path] = set()
_ROTATED_NAME = re.compile(r"^runs-(\d{8})(?:-(\d{3,}))?\.jsonl$")


def _normalise_record(value: Any) -> dict[str, Any] | None:
    """Return the public, typed subset of one JSONL record.

    Legacy three-field records remain readable, while malformed lines and
    unrelated JSON objects are ignored.  Restricting the top-level keys keeps
    this diagnostic API stable even if a log file was edited manually.
    """
    if not isinstance(value, dict):
        return None
    if not all(key in value for key in ("ts", "job", "msg")):
        return None
    fields = value.get("fields")
    try:
        version = int(value.get("v", 1))
    except (TypeError, ValueError):
        version = 1
    return {
        "v": version,
        "ts": _truncate(value.get("ts", ""), 128),
        "job": _truncate(value.get("job", ""), 512),
        "msg": _truncate(value.get("msg", ""), DEFAULT_MAX_MESSAGE_CHARS),
        "run_id": _truncate(value.get("run_id", ""), 256),
        "job_id": _truncate(value.get("job_id", ""), 256),
        "event": _truncate(value.get("event", "log"), 64),
        "level": _truncate(value.get("level", "info"), 32),
        "fields": fields if isinstance(fields, dict) else {},
    }


def recent_records(project_dir: Path, limit: int = 100) -> list[dict[str, Any]]:
    """Read the newest structured records from one project's managed logs.

    There is intentionally no filename argument: callers can only query the
    fixed ``logs/runs-*.jsonl`` namespace below the supplied project.  Symlinked
    directories/files and paths resolving outside that project are ignored.
    Results are newest-first and memory usage is bounded by ``limit``.
    """
    try:
        bounded_limit = max(1, min(int(limit), MAX_READ_RECORDS))
    except (TypeError, ValueError):
        bounded_limit = 100

    try:
        project_root = Path(project_dir).resolve()
        logs_dir = (project_root / "logs").resolve()
        logs_dir.relative_to(project_root)
        if not logs_dir.is_dir():
            return []

        candidates: list[tuple[str, int, Path]] = []
        for path in logs_dir.iterdir():
            match = _ROTATED_NAME.fullmatch(path.name)
            if not match or path.is_symlink():
                continue
            resolved = path.resolve()
            resolved.relative_to(logs_dir)
            if resolved.is_file():
                candidates.append((match.group(1), int(match.group(2) or 0), resolved))

        records: list[dict[str, Any]] = []
        for _day, _rotation, path in sorted(candidates, reverse=True):
            remaining = bounded_limit - len(records)
            if remaining <= 0:
                break
            newest_in_file: deque[dict[str, Any]] = deque(maxlen=remaining)
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        if not line.strip():
                            continue
                        try:
                            record = _normalise_record(json.loads(line))
                        except (json.JSONDecodeError, ValueError, TypeError):
                            continue
                        if record is not None:
                            newest_in_file.append(record)
            except OSError:
                continue
            records.extend(reversed(newest_in_file))
        return records[:bounded_limit]
    except (OSError, RuntimeError, ValueError):
        return []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _limits() -> tuple[int, int, int]:
    # The upper bounds also protect against a typo turning retention into an
    # effectively unbounded setting.  Operators that need more can archive the
    # JSONL files externally.
    max_files = _env_int(RUNLOG_MAX_FILES_ENV, DEFAULT_MAX_FILES, 1, 1000)
    max_bytes = _env_int(RUNLOG_MAX_BYTES_ENV, DEFAULT_MAX_BYTES, 4096, 1024**3)
    max_chars = _env_int(
        RUNLOG_MAX_MESSAGE_CHARS_ENV,
        DEFAULT_MAX_MESSAGE_CHARS,
        256,
        1024 * 1024,
    )
    return max_files, max_bytes, max_chars


def _truncate(value: Any, limit: int) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    suffix = "… [truncated]"
    return text[: max(0, limit - len(suffix))] + suffix


def _bounded_fields(fields: Mapping[str, Any] | None, limit: int) -> dict[str, Any]:
    if not fields:
        return {}
    try:
        # Round-tripping with default=str makes Paths, enums and other useful
        # diagnostic values safe without placing serialization duties on jobs.
        encoded = json.dumps(dict(fields), ensure_ascii=False, default=str)
        if len(encoded) <= limit:
            decoded = json.loads(encoded)
            return decoded if isinstance(decoded, dict) else {"value": decoded}
        return {"truncated": True, "preview": _truncate(encoded, limit)}
    except Exception:
        return {"unserializable": True}


def _line_for(
    job_name: str,
    message: str,
    *,
    run_id: str,
    job_id: str,
    event: str,
    level: str,
    fields: Mapping[str, Any] | None,
    max_bytes: int,
    max_chars: int,
) -> str:
    record: dict[str, Any] = {
        "v": 1,
        "ts": _utc_now(),
        "job": _truncate(job_name, 512),
        "msg": _truncate(message, max_chars),
        "run_id": _truncate(run_id, 256),
        "event": _truncate(event or "log", 64),
        "level": _truncate(level or "info", 32),
    }
    if job_id:
        record["job_id"] = _truncate(job_id, 256)
    safe_fields = _bounded_fields(fields, max_chars)
    if safe_fields:
        record["fields"] = safe_fields

    line = json.dumps(record, ensure_ascii=False, default=str)
    if len(line.encode("utf-8")) <= max_bytes:
        return line

    # A single pathological message/fields object must not defeat file-size
    # rotation.  Preserve correlation metadata and keep a bounded preview.
    record["msg"] = _truncate(message, max(256, max_bytes // 3))
    if "fields" in record:
        record["fields"] = {"truncated": True}
    return json.dumps(record, ensure_ascii=False, default=str)


def _next_path(logs_dir: Path, day: str, line_bytes: int, max_bytes: int) -> tuple[Path, bool]:
    cache_key = (logs_dir, day, max_bytes)
    # Keep one active entry per project.  A server that stays up for years must
    # not accumulate one cache key per project/day merely because the files do.
    for key in tuple(_active_paths):
        if key[0] == logs_dir and key != cache_key:
            _active_paths.pop(key, None)
    current = _active_paths.get(cache_key)
    if current is not None:
        try:
            if not current.exists() or current.stat().st_size + line_bytes <= max_bytes:
                return current, not current.exists()
        except OSError:
            pass

    base = logs_dir / f"runs-{day}.jsonl"
    candidates: list[tuple[int, Path]] = []
    if base.exists():
        candidates.append((0, base))
    for path in logs_dir.glob(f"runs-{day}-*.jsonl"):
        match = _ROTATED_NAME.match(path.name)
        if match and match.group(1) == day and match.group(2):
            candidates.append((int(match.group(2)), path))

    if not candidates:
        selected = base
        created = True
    else:
        index, latest = max(candidates, key=lambda item: item[0])
        try:
            fits = latest.stat().st_size + line_bytes <= max_bytes
        except OSError:
            fits = False
        if fits:
            selected = latest
            created = False
        else:
            selected = logs_dir / f"runs-{day}-{index + 1:03d}.jsonl"
            created = True

    _active_paths[cache_key] = selected
    return selected, created


def _prune_locked(logs_dir: Path, max_files: int, *, keep: Path | None = None) -> None:
    try:
        files = [path for path in logs_dir.glob("runs-*.jsonl") if path.is_file()]
        files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name))
        while len(files) > max_files:
            victim = next((path for path in files if keep is None or path != keep), None)
            if victim is None:
                break
            files.remove(victim)
            victim.unlink(missing_ok=True)
    except Exception:
        pass


def prune(project_dir: Path) -> None:
    """Apply configured retention immediately.  Never raises."""
    try:
        logs_dir = Path(project_dir) / "logs"
        if not logs_dir.exists():
            return
        max_files, _max_bytes, _max_chars = _limits()
        with _write_lock:
            _prune_locked(logs_dir, max_files)
    except Exception:
        pass


def append(
    project_dir: Path,
    job_name: str,
    message: str,
    *,
    run_id: str | None = None,
    job_id: str | None = None,
    event: str = "log",
    level: str = "info",
    fields: Mapping[str, Any] | None = None,
) -> None:
    """Append one structured log line.  The original 3-argument API remains valid."""
    try:
        logs_dir = Path(project_dir) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        max_files, max_bytes, max_chars = _limits()
        line = _line_for(
            str(job_name),
            str(message),
            run_id=str(run_id or uuid.uuid4().hex),
            job_id=str(job_id or ""),
            event=str(event),
            level=str(level),
            fields=fields,
            max_bytes=max_bytes,
            max_chars=max_chars,
        )
        encoded_size = len((line + "\n").encode("utf-8"))
        day = datetime.now().strftime("%Y%m%d")
        with _write_lock:
            path, created = _next_path(logs_dir, day, encoded_size, max_bytes)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
            # Pruning on the first write for a project and on every rollover is
            # enough to enforce the bound without globbing on every FFmpeg line.
            if created or logs_dir not in _known_logs_dirs:
                _prune_locked(logs_dir, max_files, keep=path)
                _known_logs_dirs.add(logs_dir)
    except Exception:
        pass


def tee(
    project_dir: Path,
    job_name: str,
    log: Callable[[str], None] | None,
    *,
    run_id: str | None = None,
    job_id: str | None = None,
    fields: Mapping[str, Any] | None = None,
) -> Callable[[str], None]:
    """Mirror a callback stream to one correlated run in the project log.

    Existing call sites remain single-argument callbacks.  A generated run ID
    is captured once (not once per line), so legacy SSE pipelines immediately
    gain correlation even before they pass their durable job ID explicitly.
    """
    correlated_run_id = str(run_id or job_id or uuid.uuid4().hex)

    def wrapped(message: str) -> None:
        append(
            project_dir,
            job_name,
            message,
            run_id=correlated_run_id,
            job_id=job_id,
            event="progress",
            fields=fields,
        )
        if log:
            log(message)

    # Useful to status/SSE adapters without changing the callable contract.
    setattr(wrapped, "run_id", correlated_run_id)
    return wrapped


class Run:
    """Lifecycle logger for jobs that do not expose a streaming callback.

    Entering records ``start``; leaving records ``result``, ``error`` or
    ``cancelled``.  ``outcome`` stores meaningful result details to be emitted
    only when the surrounding operation actually exits successfully.
    """

    def __init__(
        self,
        project_dir: Path,
        job_name: str,
        *,
        run_id: str | None = None,
        job_id: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ):
        self.project_dir = Path(project_dir)
        self.job_name = str(job_name)
        self.job_id = str(job_id or "")
        self.run_id = str(run_id or self.job_id or uuid.uuid4().hex)
        try:
            self.fields = dict(fields or {})
        except Exception:
            self.fields = {}
        self._started = 0.0
        self._result_message = "Completed"
        self._result_fields: dict[str, Any] = {}

    def _append(
        self,
        message: str,
        *,
        event: str,
        level: str = "info",
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            merged = {**self.fields, **dict(fields or {})}
            append(
                self.project_dir,
                self.job_name,
                message,
                run_id=self.run_id,
                job_id=self.job_id,
                event=event,
                level=level,
                fields=merged,
            )
        except Exception:
            # Keep the lifecycle wrapper fail-open even for a hostile Mapping
            # implementation passed as diagnostic metadata.
            pass

    def __enter__(self) -> "Run":
        self._started = time.monotonic()
        self._append("Started", event="start")
        return self

    def log(
        self,
        message: str,
        *,
        event: str = "progress",
        level: str = "info",
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        self._append(message, event=event, level=level, fields=fields)

    def outcome(self, message: str = "Completed", **fields: Any) -> None:
        """Set the result text/fields that will be written on successful exit."""
        try:
            self._result_message = str(message)
            self._result_fields.update(fields)
        except Exception:
            pass

    def __exit__(self, exc_type, exc, tb):
        duration_ms = max(0, round((time.monotonic() - self._started) * 1000))
        if exc_type is None:
            self._append(
                self._result_message,
                event="result",
                fields={"duration_ms": duration_ms, **self._result_fields},
            )
        elif exc_type.__name__ == "JobCancelled":
            self._append(
                str(exc or "Cancelled"),
                event="cancelled",
                level="warning",
                fields={"duration_ms": duration_ms, "error_type": exc_type.__name__},
            )
        else:
            self._append(
                str(exc or exc_type.__name__),
                event="error",
                level="error",
                fields={"duration_ms": duration_ms, "error_type": exc_type.__name__},
            )
        return False


def run(
    project_dir: Path,
    job_name: str,
    *,
    run_id: str | None = None,
    job_id: str | None = None,
    fields: Mapping[str, Any] | None = None,
) -> Run:
    """Create a lifecycle logger; intended for use as a context manager."""
    return Run(
        project_dir,
        job_name,
        run_id=run_id,
        job_id=job_id,
        fields=fields,
    )
