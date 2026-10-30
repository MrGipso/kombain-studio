"""Filesystem primitives shared by every durable store."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows refuses to replace a file while another handle has it open without
# FILE_SHARE_DELETE, which is how Python opens files for reading, and a read
# can likewise fail while a writer's replace is in flight. A UI poll and a
# worker save colliding would otherwise fail the save or return a bogus
# "missing" artifact. These collisions last milliseconds, so retry briefly.
_SHARING_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.4, 0.8)
_RETRY_SHARING_VIOLATIONS = os.name == "nt"


def replace_with_retry(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """``os.replace`` that waits out short Windows sharing violations."""
    for delay in _SHARING_RETRY_DELAYS:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if not _RETRY_SHARING_VIOLATIONS:
                raise
            time.sleep(delay)
    os.replace(source, destination)


def read_text_with_retry(path: str | os.PathLike[str], encoding: str = "utf-8") -> str:
    """``Path.read_text`` that waits out short Windows sharing violations."""
    path = Path(path)
    for delay in _SHARING_RETRY_DELAYS:
        try:
            return path.read_text(encoding=encoding)
        except PermissionError:
            if not _RETRY_SHARING_VIOLATIONS:
                raise
            time.sleep(delay)
    return path.read_text(encoding=encoding)


def preserve_corrupt_copy(path: str | os.PathLike[str]) -> Path | None:
    """Keep the bytes of an unparsable file before anything can overwrite it.

    Callers that fall back to defaults for a corrupt file would otherwise save
    those defaults over the only copy of the user's data. The copy goes to a
    sibling ``.corrupt/`` folder (outside artifact glob patterns), is named by
    content hash so repeated reads do not pile up copies, and is never
    overwritten. Returns the copy's path, or None if it could not be made.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
        backup = path.parent / ".corrupt" / f"{path.name}.{hashlib.sha256(data).hexdigest()[:12]}"
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(data)
            logger.error("Unparsable file %s preserved as %s", path, backup)
        return backup
    except OSError as exc:
        logger.error("Unparsable file %s could not be preserved: %s", path, exc)
        return None
