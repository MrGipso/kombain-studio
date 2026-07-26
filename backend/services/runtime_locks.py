"""Lifecycle-owned cross-process locks for mutable runtime roots.

The output root may be changed from Settings while the server is running.  A
plain lifespan-local lock would then protect the old root while requests start
mutating the new one.  This module keeps the active locks in one process-wide
owner and makes an output-root transition transactional with the settings save.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Iterator

from core.instance_lock import InstanceLock


_guard = threading.RLock()
_locks: dict[str, InstanceLock] = {}
_settings_key = ""
_output_key = ""
_explicit = False
_active = False


def _resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _key(path: Path | str) -> str:
    return str(_resolved(path)).casefold()


def acquire(
    settings_lock_path: Path | str,
    output_base: Path | str,
    *,
    explicit_lock_path: Path | str | None = None,
) -> None:
    """Acquire the settings/output locks for one server lifespan."""
    global _active, _explicit, _settings_key, _output_key
    with _guard:
        if _active:
            raise RuntimeError("Runtime instance locks are already active")

        if explicit_lock_path is not None:
            paths = [_resolved(explicit_lock_path)]
            settings_key = output_key = _key(paths[0])
            explicit = True
        else:
            settings_path = _resolved(settings_lock_path)
            output_path = _resolved(output_base) / ".pipeline-studio-server.lock"
            paths = [settings_path, output_path]
            settings_key = _key(settings_path)
            output_key = _key(output_path)
            explicit = False

        acquired: dict[str, InstanceLock] = {}
        try:
            for path in sorted({_key(path): path for path in paths}.values(), key=lambda item: str(item).casefold()):
                acquired[_key(path)] = InstanceLock(path).acquire()
        except BaseException:
            for instance_lock in reversed(list(acquired.values())):
                instance_lock.release()
            raise

        _locks.update(acquired)
        _settings_key = settings_key
        _output_key = output_key
        _explicit = explicit
        _active = True


def release() -> None:
    """Release every active lifespan lock."""
    global _active, _explicit, _settings_key, _output_key
    with _guard:
        for instance_lock in reversed(list(_locks.values())):
            instance_lock.release()
        _locks.clear()
        _settings_key = ""
        _output_key = ""
        _explicit = False
        _active = False


@contextmanager
def output_base_transition(output_base: Path | str) -> Iterator[None]:
    """Protect a new output root while its settings update is committed.

    The previous output lock remains held until the caller successfully saves
    the new setting.  On failure, the new lock is released and the old runtime
    contract remains intact.  Direct route unit tests do not start the ASGI
    lifespan, so an inactive manager intentionally behaves as a no-op.
    """
    global _output_key
    with _guard:
        if not _active or _explicit:
            yield
            return

        new_path = _resolved(output_base) / ".pipeline-studio-server.lock"
        new_key = _key(new_path)
        if new_key == _output_key:
            yield
            return

        acquired_new = new_key not in _locks
        if acquired_new:
            _locks[new_key] = InstanceLock(new_path).acquire()
        old_key = _output_key
        try:
            yield
        except BaseException:
            if acquired_new:
                _locks.pop(new_key).release()
            raise
        else:
            _output_key = new_key
            if old_key != _settings_key and old_key != new_key:
                old_lock = _locks.pop(old_key, None)
                if old_lock is not None:
                    old_lock.release()


def active_output_lock_path() -> Path | None:
    """Return the protected output lock path for diagnostics/tests."""
    with _guard:
        instance_lock = _locks.get(_output_key)
        return instance_lock.path if instance_lock is not None else None


__all__ = [
    "acquire",
    "active_output_lock_path",
    "output_base_transition",
    "release",
]
