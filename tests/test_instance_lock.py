from pathlib import Path

import pytest

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
