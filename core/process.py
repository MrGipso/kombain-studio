"""Shared subprocess execution with streaming logs and reliable cancellation."""

from __future__ import annotations

import subprocess
import shutil
import queue
import threading
from pathlib import Path
from collections.abc import Callable, Sequence


class ProcessCancelled(RuntimeError):
    """Raised when a boolean cancellation callback requests termination."""


def resolve_executable(configured: str, *fallback_names: str) -> str:
    """Resolve the configured executable first, then explicit PATH fallbacks.

    A configured absolute path is never silently replaced by another binary
    found on PATH.  Returning the original value when it cannot be resolved
    preserves the subprocess error that names the operator's configuration.
    """
    raw = str(configured or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        located = shutil.which(raw)
        if located:
            return str(Path(located).resolve())
    for name in fallback_names:
        located = shutil.which(name)
        if located:
            return str(Path(located).resolve())
    return raw or (fallback_names[0] if fallback_names else "")


def terminate_process(process: subprocess.Popen, timeout: float = 5.0) -> None:
    """Terminate a child and escalate to kill if it ignores the request."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def run_captured(
    command: Sequence[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a child to completion with UTF-8 output and a hard timeout.

    ffmpeg prints paths and filter text as UTF-8; decoding it with the Windows
    ANSI code page can fail mid-read. A wedged child must not hold a job
    forever: on timeout it is killed and a failed result (``returncode`` -1)
    is returned, so callers keep their usual return-code handling.
    """
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(
            list(command),
            -1,
            stdout="",
            stderr=f"{stderr}\nTimed out after {timeout:g} s",
        )


def run_streaming_process(
    command: Sequence[str],
    log_callback: Callable[[str], None] | None = None,
    *,
    cancel_check: Callable[[], object] | None = None,
    cancel_poll_seconds: float = 0.1,
) -> int:
    """Run a child, stream merged output, and terminate it on cancellation.

    Cancellation in the application is cooperative and is checked by wrapped
    log callbacks. ``cancel_check`` additionally provides polling independent
    of child output; it may raise the caller's cancellation exception or return
    a truthy value. Any callback exception is propagated after child cleanup.
    """
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    reader: threading.Thread | None = None
    try:
        if cancel_check is not None:
            output: queue.Queue[tuple[str, object]] = queue.Queue()

            def read_output() -> None:
                try:
                    for line in process.stdout:
                        output.put(("line", line))
                except BaseException as exc:
                    output.put(("error", exc))
                finally:
                    output.put(("done", ""))

            reader = threading.Thread(
                target=read_output,
                name="process-output-reader",
                daemon=True,
            )
            reader.start()
            done = False
            while not done:
                if cancel_check():
                    raise ProcessCancelled("Process cancelled")
                try:
                    kind, payload = output.get(timeout=max(0.01, cancel_poll_seconds))
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive():
                        break
                    continue
                if kind == "line":
                    if log_callback:
                        log_callback(str(payload).rstrip())
                elif kind == "error":
                    raise payload  # type: ignore[misc]
                else:
                    done = True
            if cancel_check():
                raise ProcessCancelled("Process cancelled")
            reader.join(timeout=1.0)
            return process.wait()

        for line in process.stdout:
            if log_callback:
                log_callback(line.rstrip())
        return process.wait()
    except BaseException:
        terminate_process(process)
        if reader is not None:
            reader.join(timeout=1.0)
        raise
    finally:
        process.stdout.close()
