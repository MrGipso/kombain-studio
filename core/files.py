"""Filesystem primitives shared by every durable store."""

from __future__ import annotations

import os
import time

# Windows refuses to replace a file while another handle has it open without
# FILE_SHARE_DELETE, which is how Python opens files for reading. A UI poll
# reading a JSON artifact at the moment a worker saves it would otherwise fail
# the save. Readers hold these files for milliseconds, so retry briefly.
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.4, 0.8)
_RETRY_SHARING_VIOLATIONS = os.name == "nt"


def replace_with_retry(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """``os.replace`` that waits out short Windows sharing violations."""
    for delay in _REPLACE_RETRY_DELAYS:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if not _RETRY_SHARING_VIOLATIONS:
                raise
            time.sleep(delay)
    os.replace(source, destination)
