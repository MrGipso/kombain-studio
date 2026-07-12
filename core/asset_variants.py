"""Immutable, project-local history for generated and assigned media.

The asset board remains the editable source of truth used by the existing
pipeline.  This module complements it with an append-only provenance log and a
private snapshot of every recorded media file, so regenerating a conventional
``shot_001.mp4`` never destroys the version that preceded it.

Selection is append-only too: a new event is written instead of mutating old
variant records.  The currently selected variant for a card/output field is the
last selection event for that pair.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.models import legacy_asset_card_id
from core.project import atomic_write_text, content_hash, is_within


ASSET_VARIANTS_FILE = ".asset_variants.json"
ASSET_VARIANTS_SCHEMA_VERSION = 2
OUTPUT_FIELDS: tuple[str, ...] = (
    "image_output",
    "video_output",
    "reference_output",
    "remotion_output",
    "assigned_media",
)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


class VariantError(RuntimeError):
    """Base error for variant-manifest operations."""


class VariantNotFound(VariantError):
    pass


class VariantIntegrityError(VariantError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _lock_for(project_dir: Path) -> threading.RLock:
    key = os.path.normcase(str(Path(project_dir).resolve()))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def manifest_path(project_dir: Path | str) -> Path:
    return Path(project_dir) / ASSET_VARIANTS_FILE


def _empty_manifest() -> dict[str, Any]:
    return {
        "schema_version": ASSET_VARIANTS_SCHEMA_VERSION,
        "revision": 0,
        "created_at": None,
        "updated_at": None,
        "variants": [],
        "selection_events": [],
    }


def load_manifest(project_dir: Path | str) -> dict[str, Any]:
    """Read a manifest without modifying the project.

    Unknown top-level keys are retained for forward compatibility.  Invalid
    records are ignored by readers rather than making the asset board unusable.
    """
    path = manifest_path(project_dir)
    if not path.is_file():
        return _empty_manifest()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_manifest()
    if not isinstance(raw, dict):
        return _empty_manifest()

    result = dict(raw)
    try:
        version = max(0, int(result.get("schema_version", 0)))
    except (TypeError, ValueError):
        version = 0
    result["schema_version"] = version
    try:
        result["revision"] = max(0, int(result.get("revision", 0)))
    except (TypeError, ValueError):
        result["revision"] = 0
    result.setdefault("created_at", None)
    result.setdefault("updated_at", None)
    result["variants"] = [item for item in result.get("variants", []) if isinstance(item, dict)]
    result["selection_events"] = [
        item for item in result.get("selection_events", []) if isinstance(item, dict)
    ]
    return result


def _load_manifest_for_update(project_dir: Path | str) -> dict[str, Any]:
    """Refuse to overwrite a damaged existing history during an update."""
    path = manifest_path(project_dir)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VariantIntegrityError(f"Asset variant manifest is unreadable: {path}") from exc
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("variants", []), list)
            or not isinstance(raw.get("selection_events", []), list)
        ):
            raise VariantIntegrityError(f"Asset variant manifest has an invalid structure: {path}")
    return load_manifest(project_dir)


def _save_manifest(project_dir: Path, manifest: dict[str, Any]) -> None:
    now = _utc_now()
    stored = dict(manifest)
    try:
        loaded_version = int(stored.get("schema_version", 0))
    except (TypeError, ValueError):
        loaded_version = 0
    stored["schema_version"] = max(ASSET_VARIANTS_SCHEMA_VERSION, loaded_version)
    stored["revision"] = max(0, int(stored.get("revision", 0))) + 1
    stored["created_at"] = stored.get("created_at") or now
    stored["updated_at"] = now
    atomic_write_text(
        manifest_path(project_dir),
        json.dumps(stored, ensure_ascii=False, indent=2, sort_keys=True),
    )


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _get(value: Any, key: str, default: Any = "") -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _VIDEO_EXTENSIONS:
        return "video"
    if suffix in _AUDIO_EXTENSIONS:
        return "audio"
    return "file"


def _resolve_input_path(project_dir: Path, value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        project_candidate = project_dir / path
        path = project_candidate if project_candidate.exists() else path
    return path.resolve()


def _snapshot_file(project_dir: Path, source: Path, variant_id: str, shot_index: int,
                   output_field: str) -> Path:
    suffix = source.suffix.lower()
    destination = (
        project_dir
        / "assets"
        / "variants"
        / f"shot_{max(0, int(shot_index)):03d}"
        / f"{variant_id}_{output_field}{suffix}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(destination.parent), prefix=destination.name + ".", suffix=".tmp"
    )
    os.close(fd)
    try:
        shutil.copyfile(source, tmp_name)
        # Windows only permits fsync on a descriptor opened for writing.
        with open(tmp_name, "r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, destination)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return destination.resolve()


def _record_card_id(record: dict[str, Any]) -> str:
    return str(record.get("card_id", "") or "").strip()


def _record_card_index(record: dict[str, Any]) -> int | None:
    try:
        return int(record.get("card_index"))
    except (TypeError, ValueError):
        return None


def _identity_matches(
    record: dict[str, Any],
    *,
    card_id: str | None,
    card_index: int | None,
) -> bool:
    """Match by stable identity, falling back to index for schema-v1 rows."""
    record_id = _record_card_id(record)
    target_id = str(card_id or "").strip()
    if target_id and record_id:
        return record_id == target_id
    if card_index is None:
        return not target_id or record_id == target_id
    return _record_card_index(record) == int(card_index)


def _current_selection_ids(
    manifest: dict[str, Any],
    card_index: int | None = None,
    card_id: str | None = None,
) -> dict[str, str | None]:
    selected: dict[str, str | None] = {}
    for event in manifest.get("selection_events", []):
        if not _identity_matches(event, card_id=card_id, card_index=card_index):
            continue
        field = str(event.get("output_field", ""))
        if field not in OUTPUT_FIELDS:
            continue
        selected[field] = event.get("variant_id") or None
    return {field: selected.get(field) for field in OUTPUT_FIELDS}


def current_selections(
    project_dir: Path | str,
    card_index: int,
    *,
    card_id: str | None = None,
) -> dict[str, str | None]:
    return _current_selection_ids(
        load_manifest(project_dir), int(card_index), card_id=card_id
    )


def _variant_by_id(manifest: dict[str, Any], variant_id: str) -> dict[str, Any] | None:
    for variant in manifest.get("variants", []):
        if variant.get("id") == variant_id:
            return variant
    return None


def get_variant(project_dir: Path | str, variant_id: str) -> dict[str, Any]:
    manifest = load_manifest(project_dir)
    variant = _variant_by_id(manifest, variant_id)
    if variant is None:
        raise VariantNotFound(f"Asset variant not found: {variant_id}")
    result = dict(variant)
    variant_index = _record_card_index(result)
    selections = _current_selection_ids(
        manifest,
        variant_index,
        card_id=_record_card_id(result) or None,
    )
    result["selected"] = selections.get(str(result.get("output_field", ""))) == result.get("id")
    return result


def list_variants(project_dir: Path | str, *, card_index: int | None = None,
                  card_id: str | None = None,
                  shot_index: int | None = None) -> list[dict[str, Any]]:
    manifest = load_manifest(project_dir)
    result: list[dict[str, Any]] = []
    for item in manifest.get("variants", []):
        try:
            item_card = int(item.get("card_index"))
            item_shot = int(item.get("shot_index"))
        except (TypeError, ValueError):
            continue
        if (card_index is not None or card_id) and not _identity_matches(
            item, card_id=card_id, card_index=card_index
        ):
            continue
        if shot_index is not None and item_shot != int(shot_index):
            continue
        variant = dict(item)
        selections = _current_selection_ids(
            manifest,
            item_card,
            card_id=_record_card_id(item) or None,
        )
        variant["selected"] = (
            selections.get(str(variant.get("output_field", ""))) == variant.get("id")
        )
        result.append(variant)
    return result


def card_variant_payload(project_dir: Path | str, card_index: int,
                         shot_index: int | None = None,
                         card_id: str | None = None) -> dict[str, Any]:
    variants = list_variants(project_dir, card_index=card_index, card_id=card_id)
    if shot_index is None:
        shot_index = int(variants[-1].get("shot_index", 0)) if variants else 0
    return {
        "card_id": str(card_id or ""),
        "card_index": int(card_index),
        "shot_index": int(shot_index),
        "selections": current_selections(
            project_dir, int(card_index), card_id=card_id
        ),
        "variants": variants,
    }


def _workflow_info(card: Any, output_field: str, settings: Any) -> tuple[str, str]:
    workflow = ""
    if output_field == "image_output":
        model = str(_get(card, "image_model", "")).lower()
        key = "comfy_ideogram_workflow" if "ideogram" in model else "comfy_krea_workflow"
        workflow = str(_get(settings, key, "") or "")
    elif output_field == "video_output":
        draft = bool(_get(card, "ltx_draft", False))
        key = "comfy_ltx_draft_workflow" if draft else "comfy_ltx_workflow"
        workflow = str(_get(settings, key, "") or "")
    elif output_field == "remotion_output":
        workflow = str(_get(card, "remotion_template", "") or "")
    if not workflow:
        return "", ""
    workflow_path = Path(workflow).expanduser()
    try:
        digest = content_hash(workflow_path) if workflow_path.is_file() else content_hash(workflow)
    except OSError:
        digest = content_hash(workflow)
    return workflow, digest


def _settings_hash(settings: Any) -> str:
    if settings is None:
        return ""
    if is_dataclass(settings) and not isinstance(settings, type):
        value: Any = asdict(settings)
    elif hasattr(settings, "model_dump"):
        value = settings.model_dump(mode="json")
    elif hasattr(settings, "__dict__"):
        value = dict(vars(settings))
    else:
        value = settings
    if isinstance(value, dict):
        sensitive = ("api_key", "apikey", "secret", "token", "password", "credential")
        value = {
            key: item
            for key, item in value.items()
            if not any(marker in str(key).lower() for marker in sensitive)
        }
    return content_hash(_json_safe(value))


def _model_hash(model: str) -> str:
    if not model:
        return ""
    path = Path(model).expanduser()
    try:
        return content_hash(path) if path.is_file() else content_hash(model)
    except OSError:
        return content_hash(model)


def _bind_manifest_identities(
    manifest: dict[str, Any], identities: dict[int, str]
) -> bool:
    """Attach stable ids to legacy records without rewriting their history."""
    normalized = {
        int(index): str(card_id).strip()
        for index, card_id in identities.items()
        if str(card_id or "").strip()
    }
    changed = int(manifest.get("schema_version", 0) or 0) < ASSET_VARIANTS_SCHEMA_VERSION
    for collection in ("variants", "selection_events"):
        for record in manifest.get(collection, []):
            if not isinstance(record, dict) or _record_card_id(record):
                continue
            index = _record_card_index(record)
            if index is not None and index in normalized:
                record["card_id"] = normalized[index]
                changed = True
    return changed


def bind_card_identities(
    project_dir: Path | str, identities: dict[int, str]
) -> bool:
    """Upgrade schema-v1 rows using their last known board positions.

    The operation is append-history-safe: variants and selection events retain
    every existing field and only gain ``card_id``.  Unknown manifest keys are
    preserved by the normal writer.
    """
    project_dir = Path(project_dir).resolve()
    if not manifest_path(project_dir).is_file():
        return False
    with _lock_for(project_dir):
        manifest = _load_manifest_for_update(project_dir)
        if not _bind_manifest_identities(manifest, identities):
            return False
        _save_manifest(project_dir, manifest)
    return True


def bind_card_identity(project_dir: Path | str, card_index: int, card_id: str) -> bool:
    return bind_card_identities(project_dir, {int(card_index): card_id})


def _append_selection_event(manifest: dict[str, Any], *, card_index: int,
                            card_id: str,
                            output_field: str, variant_id: str | None,
                            reason: str, parent_id: str | None) -> dict[str, Any]:
    event = {
        "id": "sel_" + uuid.uuid4().hex,
        "created_at": _utc_now(),
        "card_id": str(card_id),
        "card_index": int(card_index),
        "output_field": output_field,
        "variant_id": variant_id,
        "parent_id": parent_id,
        "reason": reason,
    }
    manifest.setdefault("selection_events", []).append(event)
    return event


def record_variant(
    project_dir: Path | str,
    *,
    card_index: int,
    shot_index: int,
    source: str,
    mode: str,
    output_field: str,
    file_path: Path | str,
    prompt: str = "",
    query: str = "",
    model: str = "",
    settings: Any = None,
    workflow: str = "",
    workflow_hash: str = "",
    metadata: dict[str, Any] | None = None,
    select: bool = True,
    card_id: str = "",
) -> dict[str, Any]:
    """Snapshot one file and append its immutable provenance record."""
    project_dir = Path(project_dir).resolve()
    if output_field not in OUTPUT_FIELDS:
        raise ValueError(f"Unsupported asset output field: {output_field}")
    source_path = _resolve_input_path(project_dir, file_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    stable_card_id = str(card_id or "").strip() or legacy_asset_card_id(
        project_dir, card_index, shot_index
    )

    with _lock_for(project_dir):
        manifest = _load_manifest_for_update(project_dir)
        _bind_manifest_identities(manifest, {int(card_index): stable_card_id})
        selections = _current_selection_ids(
            manifest, int(card_index), card_id=stable_card_id
        )
        parent_id = selections.get(output_field)
        variant_id = "var_" + uuid.uuid4().hex
        snapshot = _snapshot_file(project_dir, source_path, variant_id, shot_index, output_field)
        try:
            file_digest = content_hash(snapshot)
            variant_metadata = dict(metadata or {})
            variant_metadata.setdefault("original_path", str(source_path))
            variant = {
                "id": variant_id,
                "created_at": _utc_now(),
                "card_id": stable_card_id,
                "card_index": int(card_index),
                "shot_index": int(shot_index),
                "source": str(source or "unknown"),
                "mode": str(mode or "unknown"),
                "output_field": output_field,
                "file_path": str(snapshot),
                "file_hash": file_digest,
                "file_size": snapshot.stat().st_size,
                "media_type": _media_type(snapshot),
                "parent_id": parent_id,
                "prompt": str(prompt or ""),
                "query": str(query or ""),
                "model": str(model or ""),
                "settings_hash": _settings_hash(settings),
                "model_hash": _model_hash(str(model)),
                "workflow": str(workflow or ""),
                "workflow_hash": str(workflow_hash or ""),
                "metadata": _json_safe(variant_metadata),
            }
            manifest.setdefault("variants", []).append(variant)
            if select:
                _append_selection_event(
                    manifest,
                    card_index=card_index,
                    card_id=stable_card_id,
                    output_field=output_field,
                    variant_id=variant_id,
                    reason="recorded",
                    parent_id=parent_id,
                )
            _save_manifest(project_dir, manifest)
        except Exception:
            try:
                snapshot.unlink()
            except OSError:
                pass
            raise
    result = dict(variant)
    result["selected"] = bool(select)
    return result


def record_card_outputs(
    project_dir: Path | str,
    *,
    card_index: int,
    card: Any,
    source: str,
    mode: str,
    settings: Any = None,
    output_fields: Iterable[str] | None = None,
    metadata: dict[str, Any] | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Record selected card outputs, de-duplicating ordinary board saves.

    ``force=True`` is intended for a completed generation attempt: even an
    identical render is still a separate, auditable attempt.  Manual/bulk saves
    use the default and simply select a known snapshot when possible.
    """
    project_dir = Path(project_dir).resolve()
    fields = tuple(output_fields or OUTPUT_FIELDS)
    shot_index = int(_get(card, "shot_index", card_index + 1) or card_index + 1)
    card_id = str(_get(card, "card_id", "") or "").strip() or legacy_asset_card_id(
        project_dir, card_index, shot_index
    )
    common_metadata = {
        "route": _get(card, "route", ""),
        "visual_type": _get(card, "visual_type", ""),
        "source_url": _get(card, "source_url", ""),
        "source_license": _get(card, "source_license", ""),
        "source_author": _get(card, "source_author", ""),
        "stock_score": _get(card, "stock_score", 0.0),
        "reference_url": _get(card, "reference_url", ""),
        "reference_source": _get(card, "reference_source", ""),
        "reference_credit": _get(card, "reference_credit", ""),
        "qa_score": _get(card, "qa_score", -1.0),
        "ltx_draft": bool(_get(card, "ltx_draft", False)),
    }
    common_metadata.update(metadata or {})
    recorded: list[dict[str, Any]] = []

    for output_field in fields:
        if output_field not in OUTPUT_FIELDS:
            raise ValueError(f"Unsupported asset output field: {output_field}")
        value = str(_get(card, output_field, "") or "").strip()
        if not value:
            continue
        try:
            source_path = _resolve_input_path(project_dir, value)
        except OSError:
            continue
        if not source_path.is_file():
            continue
        digest = content_hash(source_path)

        if not force:
            manifest = load_manifest(project_dir)
            selected_id = _current_selection_ids(
                manifest, int(card_index), card_id=card_id
            ).get(output_field)
            selected_variant = _variant_by_id(manifest, selected_id) if selected_id else None
            if selected_variant and selected_variant.get("file_hash") == digest:
                continue
            known = next(
                (
                    item for item in reversed(manifest.get("variants", []))
                    if _identity_matches(
                        item, card_id=card_id, card_index=int(card_index)
                    )
                    and item.get("output_field") == output_field
                    and item.get("file_hash") == digest
                ),
                None,
            )
            if known:
                select_variant(project_dir, known["id"], reason=f"{source}:{mode}")
                recorded.append(get_variant(project_dir, known["id"]))
                continue

        if output_field == "image_output":
            prompt = str(_get(card, "image_prompt", "") or "")
            model = str(_get(card, "image_model", "") or "")
        elif output_field == "video_output":
            prompt = str(_get(card, "video_prompt", "") or "")
            model = str(_get(card, "video_model", "") or "")
        elif output_field == "remotion_output":
            prompt = str(_get(card, "screen_text", "") or "")
            model = str(_get(card, "remotion_template", "") or "")
        else:
            prompt = ""
            model = ""
        query = str(
            _get(card, "reference_query", "")
            or _get(card, "asset_query", "")
            or _get(card, "description", "")
            or ""
        )
        workflow, workflow_digest = _workflow_info(card, output_field, settings)
        recorded.append(
            record_variant(
                project_dir,
                card_index=card_index,
                shot_index=shot_index,
                source=source,
                mode=mode,
                output_field=output_field,
                file_path=source_path,
                prompt=prompt,
                query=query,
                model=model,
                settings=settings,
                workflow=workflow,
                workflow_hash=workflow_digest,
                metadata=common_metadata,
                card_id=card_id,
            )
        )
    return recorded


def verify_variant(
    variant: dict[str, Any], project_dir: Path | str | None = None
) -> Path:
    path = Path(str(variant.get("file_path", "")))
    if project_dir is not None:
        project_root = Path(project_dir).resolve()
        if not path.is_absolute():
            path = project_root / path
        try:
            path = path.resolve()
        except OSError:
            pass
        if not is_within(path, project_root / "assets" / "variants"):
            raise VariantIntegrityError(
                f"Variant media is outside the immutable store: {variant.get('id', '')}"
            )
    if not path.is_file():
        raise VariantIntegrityError(f"Variant media is missing: {path}")
    expected = str(variant.get("file_hash", "") or "").strip().lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise VariantIntegrityError(
            f"Variant media has no valid SHA-256: {variant.get('id', '')}"
        )
    if content_hash(path) != expected:
        raise VariantIntegrityError(f"Variant media hash mismatch: {variant.get('id', '')}")
    return path


def select_variant(project_dir: Path | str, variant_id: str, *,
                   reason: str = "api", card_id: str | None = None,
                   card_index: int | None = None) -> dict[str, Any]:
    """Verify a snapshot and append a selection event for it."""
    project_dir = Path(project_dir).resolve()
    with _lock_for(project_dir):
        manifest = _load_manifest_for_update(project_dir)
        variant = _variant_by_id(manifest, variant_id)
        if variant is None:
            raise VariantNotFound(f"Asset variant not found: {variant_id}")
        verify_variant(variant, project_dir)
        variant_index = int(variant["card_index"])
        variant_card_id = _record_card_id(variant)
        if (
            int(manifest.get("schema_version", 0) or 0) >= 2
            and not variant_card_id
        ):
            raise VariantIntegrityError(
                f"Schema-v2 variant has no stable card_id: {variant_id}"
            )
        requested_card_id = str(card_id or "").strip()
        if variant_card_id and requested_card_id and variant_card_id != requested_card_id:
            raise VariantNotFound(f"Asset variant not found: {variant_id}")
        active_card_id = variant_card_id or requested_card_id or legacy_asset_card_id(
            project_dir, variant_index, variant.get("shot_index", "")
        )
        active_card_index = variant_index if card_index is None else int(card_index)
        # A legacy variant is bound using its historical index before a new
        # selection event records the card's current display position.
        changed = _bind_manifest_identities(manifest, {variant_index: active_card_id})
        variant = _variant_by_id(manifest, variant_id) or variant
        output_field = str(variant["output_field"])
        parent_id = _current_selection_ids(
            manifest, active_card_index, card_id=active_card_id
        ).get(output_field)
        if parent_id != variant_id:
            _append_selection_event(
                manifest,
                card_index=active_card_index,
                card_id=active_card_id,
                output_field=output_field,
                variant_id=variant_id,
                reason=reason,
                parent_id=parent_id,
            )
            changed = True
        if changed:
            _save_manifest(project_dir, manifest)
    return get_variant(project_dir, variant_id)


def deselect_output(project_dir: Path | str, card_index: int, output_field: str,
                    *, reason: str = "manual", card_id: str | None = None) -> None:
    """Append an explicit deselection when a board output is cleared."""
    if output_field not in OUTPUT_FIELDS:
        raise ValueError(f"Unsupported asset output field: {output_field}")
    project_dir = Path(project_dir).resolve()
    stable_card_id = str(card_id or "").strip() or legacy_asset_card_id(
        project_dir, card_index
    )
    with _lock_for(project_dir):
        manifest = _load_manifest_for_update(project_dir)
        changed = _bind_manifest_identities(
            manifest, {int(card_index): stable_card_id}
        )
        parent_id = _current_selection_ids(
            manifest, int(card_index), card_id=stable_card_id
        ).get(output_field)
        if parent_id is None:
            if changed:
                _save_manifest(project_dir, manifest)
            return
        _append_selection_event(
            manifest,
            card_index=card_index,
            card_id=stable_card_id,
            output_field=output_field,
            variant_id=None,
            reason=reason,
            parent_id=parent_id,
        )
        _save_manifest(project_dir, manifest)
