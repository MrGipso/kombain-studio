"""Append-only, hash-bound QA evidence for generated asset media.

The editable Asset Board is intentionally not the source of truth for QA.  A
card can be regenerated at the same conventional path, so a scalar stored on
the card can silently describe bytes that no longer exist.  This sidecar keeps
every report and binds it to the SHA-256 of the exact image/video inspected.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.project import atomic_write_text


ASSET_QA_FILE = ".asset_qa.json"
ASSET_QA_SCHEMA_VERSION = 1

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


class QAStoreError(RuntimeError):
    """Base error for the project-local QA evidence store."""


class QAStoreIntegrityError(QAStoreError):
    """An existing store is damaged and must not be overwritten silently."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _lock_for(project_dir: Path) -> threading.RLock:
    key = os.path.normcase(str(Path(project_dir).resolve()))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def store_path(project_dir: Path | str) -> Path:
    return Path(project_dir) / ASSET_QA_FILE


def _empty_store() -> dict[str, Any]:
    return {
        "schema_version": ASSET_QA_SCHEMA_VERSION,
        "revision": 0,
        "created_at": None,
        "updated_at": None,
        "reports": [],
    }


def _normalise(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty_store()
    result = dict(raw)
    try:
        result["schema_version"] = max(0, int(result.get("schema_version", 0)))
    except (TypeError, ValueError):
        result["schema_version"] = 0
    try:
        result["revision"] = max(0, int(result.get("revision", 0)))
    except (TypeError, ValueError):
        result["revision"] = 0
    result.setdefault("created_at", None)
    result.setdefault("updated_at", None)
    reports = result.get("reports", [])
    result["reports"] = [item for item in reports if isinstance(item, dict)] if isinstance(reports, list) else []
    return result


def load_store(project_dir: Path | str) -> dict[str, Any]:
    """Read QA history without modifying the project.

    Like the variant history reader, this stays tolerant for UI reads.  The
    update path below is strict so a corrupt audit trail is never erased.
    """
    path = store_path(project_dir)
    if not path.is_file():
        return _empty_store()
    try:
        return _normalise(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return _empty_store()


def _load_for_update(project_dir: Path) -> dict[str, Any]:
    path = store_path(project_dir)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QAStoreIntegrityError(f"QA store is unreadable: {path}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("reports", []), list):
            raise QAStoreIntegrityError(f"QA store has an invalid structure: {path}")
        return _normalise(raw)
    return _empty_store()


def append_report(project_dir: Path | str, report: dict[str, Any]) -> dict[str, Any]:
    """Append one JSON-safe report and return the stored record."""
    project_dir = Path(project_dir).resolve()
    # Round-trip rejects unserialisable values and guarantees no accidental
    # bytes/base64 handles leak into the audit file.
    stored_report = json.loads(json.dumps(report, ensure_ascii=False))
    if not isinstance(stored_report, dict):
        raise TypeError("QA report must be a JSON object")
    if not str(stored_report.get("card_id", "")).strip():
        raise ValueError("QA report requires card_id")
    if stored_report.get("kind") not in {"image", "video"}:
        raise ValueError("QA report kind must be image or video")
    media = stored_report.get("media")
    digest = str(media.get("sha256", "")) if isinstance(media, dict) else ""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ValueError("QA report requires a 64-character media.sha256")

    now = _utc_now()
    stored_report.setdefault("report_id", uuid.uuid4().hex)
    stored_report.setdefault("created_at", now)
    with _lock_for(project_dir):
        store = _load_for_update(project_dir)
        store.setdefault("reports", []).append(stored_report)
        store["schema_version"] = max(
            ASSET_QA_SCHEMA_VERSION,
            int(store.get("schema_version", 0) or 0),
        )
        store["revision"] = max(0, int(store.get("revision", 0) or 0)) + 1
        store["created_at"] = store.get("created_at") or now
        store["updated_at"] = now
        atomic_write_text(
            store_path(project_dir),
            json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True),
        )
    return dict(stored_report)


def reports_for_card(project_dir: Path | str, card_id: str) -> list[dict[str, Any]]:
    stable_id = str(card_id or "").strip()
    return [
        dict(item)
        for item in load_store(project_dir).get("reports", [])
        if str(item.get("card_id", "")).strip() == stable_id
    ]


def latest_reports(project_dir: Path | str) -> dict[str, dict[str, dict[str, Any]]]:
    """Return latest report per card and media kind (image/video)."""
    latest: dict[str, dict[str, dict[str, Any]]] = {}
    for item in load_store(project_dir).get("reports", []):
        card_id = str(item.get("card_id", "")).strip()
        kind = str(item.get("kind", "")).strip()
        if card_id and kind in {"image", "video"}:
            latest.setdefault(card_id, {})[kind] = dict(item)
    return latest
