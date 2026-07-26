from pathlib import Path

import pytest

from backend.services import runtime_locks
from core.instance_lock import InstanceAlreadyRunning, InstanceLock


def test_instance_lock_excludes_a_second_process_handle(tmp_path: Path):
    path = tmp_path / "server.lock"
    first = InstanceLock(path).acquire()
    try:
        with pytest.raises(InstanceAlreadyRunning):
            InstanceLock(path).acquire()
    finally:
        first.release()

    second = InstanceLock(path).acquire()
    second.release()


def test_runtime_output_lock_transition_holds_both_roots_until_commit(tmp_path: Path):
    settings_lock = tmp_path / "runtime" / "server.lock"
    old_root = tmp_path / "old-projects"
    new_root = tmp_path / "new-projects"
    old_lock_path = old_root / ".pipeline-studio-server.lock"
    new_lock_path = new_root / ".pipeline-studio-server.lock"

    runtime_locks.acquire(settings_lock, old_root)
    try:
        assert runtime_locks.active_output_lock_path() == old_lock_path.resolve()
        with runtime_locks.output_base_transition(new_root):
            with pytest.raises(InstanceAlreadyRunning):
                InstanceLock(old_lock_path).acquire()
            with pytest.raises(InstanceAlreadyRunning):
                InstanceLock(new_lock_path).acquire()

        assert runtime_locks.active_output_lock_path() == new_lock_path.resolve()
        old_probe = InstanceLock(old_lock_path).acquire()
        old_probe.release()
        with pytest.raises(InstanceAlreadyRunning):
            InstanceLock(new_lock_path).acquire()
    finally:
        runtime_locks.release()


def test_runtime_output_lock_transition_rolls_back_when_save_fails(tmp_path: Path):
    settings_lock = tmp_path / "runtime" / "server.lock"
    old_root = tmp_path / "old-projects"
    new_root = tmp_path / "new-projects"
    new_lock_path = new_root / ".pipeline-studio-server.lock"

    runtime_locks.acquire(settings_lock, old_root)
    try:
        with pytest.raises(RuntimeError, match="save failed"):
            with runtime_locks.output_base_transition(new_root):
                raise RuntimeError("save failed")

        assert runtime_locks.active_output_lock_path() == (
            old_root / ".pipeline-studio-server.lock"
        ).resolve()
        new_probe = InstanceLock(new_lock_path).acquire()
        new_probe.release()
    finally:
        runtime_locks.release()
