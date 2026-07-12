"""Master-export provenance verification and immutable delivery manifests.

The editable asset board tells the exporter which media path to consume, while
``core.asset_variants`` keeps the append-only history behind those paths.  A
professional Master must bind both views together: every included board card
has to point at the bytes of its explicitly selected immutable variant.

This module is deliberately read-only during preflight.  It never repairs or
reselects variants behind the editor's back.  After a successful technical QC,
it can write one create-only JSON sidecar next to the MP4.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.asset_variants import ASSET_VARIANTS_FILE, OUTPUT_FIELDS
from core.export import (
    EXCLUDED_STATUSES,
    VIDEO_ROUTES,
    card_route,
    find_voice_audio,
    resolve_music_track,
)
from core.project import atomic_write_text, content_hash, is_within
from core.version import __version__
from core.process import resolve_executable


MASTER_MANIFEST_SCHEMA_VERSION = 1
MASTER_MANIFEST_TYPE = "pipeline-studio.master-export"
MASTER_EXPORT_INDEX_FILE = ".master_exports.json"
MASTER_EXPORT_INDEX_SCHEMA_VERSION = 1


class ExportManifestError(RuntimeError):
    """Base error for Master manifest operations."""


class ExportManifestExists(ExportManifestError):
    """Raised when a caller attempts to replace an immutable sidecar."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _get(value: Any, key: str, default: Any = "") -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _portable_path(path: Path, project_dir: Path) -> str:
    """Return a portable logical path without leaking host filesystem layout."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return f"external/{resolved.name}"


def chosen_card_media(card: Any) -> tuple[str, Path | None, str]:
    """Return ``(output_field, path, route)`` exactly as ``core.export`` does."""
    route = card_route(card)
    assigned = str(_get(card, "assigned_media", "") or "").strip()
    if assigned and Path(assigned).is_file():
        return "assigned_media", Path(assigned).resolve(), route

    output_field = "video_output" if route in VIDEO_ROUTES else "image_output"
    candidate = str(_get(card, output_field, "") or "").strip()
    if candidate and Path(candidate).is_file():
        return output_field, Path(candidate).resolve(), route
    return output_field, None, route


def _strict_variant_manifest(project_dir: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Load the variant manifest without silently normalising damaged history."""
    path = project_dir / ASSET_VARIANTS_FILE
    if not path.is_file():
        return None, {
            "code": "missing_provenance",
            "message": "Для Master отсутствует журнал версий медиа",
            "item": ASSET_VARIANTS_FILE,
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, {
            "code": "corrupt_provenance_manifest",
            "message": "Журнал версий медиа повреждён или недоступен",
            "item": ASSET_VARIANTS_FILE,
            "details": {"error": str(exc)},
        }
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("variants"), list)
        or not isinstance(raw.get("selection_events"), list)
    ):
        return None, {
            "code": "corrupt_provenance_manifest",
            "message": "Журнал версий медиа имеет неверную структуру",
            "item": ASSET_VARIANTS_FILE,
        }
    schema_version = _integer(raw.get("schema_version")) or 0
    for collection in ("variants", "selection_events"):
        seen_ids: set[str] = set()
        for record in raw.get(collection, []):
            if not isinstance(record, dict):
                return None, {
                    "code": "corrupt_provenance_manifest",
                    "message": "Журнал версий медиа содержит запись неверного типа",
                    "item": ASSET_VARIANTS_FILE,
                    "details": {"collection": collection},
                }
            record_id = str(record.get("id", "") or "").strip()
            if not record_id or record_id in seen_ids:
                return None, {
                    "code": "corrupt_provenance_manifest",
                    "message": "Журнал версий медиа содержит пустой или повторяющийся ID",
                    "item": ASSET_VARIANTS_FILE,
                    "details": {"collection": collection, "record_id": record_id},
                }
            seen_ids.add(record_id)
            if schema_version >= 2 and not str(record.get("card_id", "") or "").strip():
                return None, {
                    "code": "corrupt_provenance_manifest",
                    "message": "Журнал версий schema v2 содержит запись без card_id",
                    "item": ASSET_VARIANTS_FILE,
                    "details": {"collection": collection, "record_id": record_id},
                }
    return raw, None


def _identity_matches(
    record: dict[str, Any],
    card_id: str,
    card_index: int,
    *,
    allow_legacy_position: bool,
) -> bool:
    """Match stable identities; only schema-v1 rows may fall back to position."""
    record_id = str(record.get("card_id", "") or "").strip()
    if record_id:
        return record_id == card_id
    return allow_legacy_position and _integer(record.get("card_index")) == card_index


def _selected_variant_id(
    manifest: dict[str, Any], card_id: str, card_index: int, output_field: str
) -> str | None:
    selected: str | None = None
    seen = False
    allow_legacy_position = (_integer(manifest.get("schema_version")) or 0) < 2
    for event in manifest.get("selection_events", []):
        if not isinstance(event, dict) or not _identity_matches(
            event,
            card_id,
            card_index,
            allow_legacy_position=allow_legacy_position,
        ):
            continue
        if str(event.get("output_field", "")) != output_field:
            continue
        seen = True
        selected = str(event.get("variant_id", "") or "").strip() or None
    return selected if seen else None


def _variant_by_id(manifest: dict[str, Any], variant_id: str) -> dict[str, Any] | None:
    for item in manifest.get("variants", []):
        if isinstance(item, dict) and str(item.get("id", "")) == variant_id:
            return item
    return None


def _reason(
    code: str,
    message: str,
    *,
    card_index: int,
    shot_index: int | None,
    card_id: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "card_index": card_index,
        "shot_index": shot_index,
        "item": card_id,
    }
    if details:
        payload["details"] = details
    return payload


def evaluate_master_provenance(
    project_dir: Path | str,
    cards: Iterable[Any],
) -> dict[str, Any]:
    """Verify selected variants for every card the exporter will include.

    The current board media may be the immutable snapshot itself or another
    project-local copy.  Equality is defined by SHA-256 bytes, never by path.
    The returned card records are frozen input for the post-render sidecar.
    """
    project_dir = Path(project_dir).resolve()
    manifest, manifest_error = _strict_variant_manifest(project_dir)
    if manifest_error:
        return {
            "ok": False,
            "schema_version": MASTER_MANIFEST_SCHEMA_VERSION,
            "variant_manifest": None,
            "cards": [],
            "reasons": [manifest_error],
        }

    assert manifest is not None
    manifest_file = project_dir / ASSET_VARIANTS_FILE
    manifest_contract = {
        "path": ASSET_VARIANTS_FILE,
        "sha256": content_hash(manifest_file),
        "schema_version": _integer(manifest.get("schema_version")) or 0,
        "revision": _integer(manifest.get("revision")) or 0,
    }
    reasons: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    for card_index, card in enumerate(cards):
        status = str(_get(card, "status", "") or "").strip().lower()
        if status in EXCLUDED_STATUSES:
            continue
        output_field, media_path, route = chosen_card_media(card)
        # The ordinary gate owns the user-facing missing-media error.  There are
        # no bytes to bind to provenance until that blocker is fixed.
        if media_path is None:
            continue

        shot_index = _integer(_get(card, "shot_index", None))
        card_id = str(_get(card, "card_id", "") or "").strip()
        label = f"Shot {shot_index}" if shot_index is not None else f"Card {card_index + 1}"
        if not card_id:
            reasons.append(_reason(
                "missing_card_identity",
                f"{label}: отсутствует стабильный card_id для provenance",
                card_index=card_index,
                shot_index=shot_index,
            ))
            continue
        if not is_within(media_path, project_dir):
            reasons.append(_reason(
                "export_media_outside_project",
                f"{label}: Master использует медиа вне каталога проекта",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
                details={"output_field": output_field, "route": route},
            ))
            continue
        if output_field not in OUTPUT_FIELDS:
            reasons.append(_reason(
                "unsupported_provenance_field",
                f"{label}: поле {output_field} не поддерживает журнал версий",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
            ))
            continue

        variant_id = _selected_variant_id(manifest, card_id, card_index, output_field)
        if not variant_id:
            reasons.append(_reason(
                "missing_selected_variant",
                f"{label}: для {output_field} не выбрана сохранённая версия",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
                details={"output_field": output_field, "route": route},
            ))
            continue
        variant = _variant_by_id(manifest, variant_id)
        if variant is None:
            reasons.append(_reason(
                "selected_variant_missing",
                f"{label}: выбранная версия {variant_id} отсутствует в журнале",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
                details={"variant_id": variant_id, "output_field": output_field},
            ))
            continue

        variant_card_id = str(variant.get("card_id", "") or "").strip()
        if variant_card_id and variant_card_id != card_id:
            reasons.append(_reason(
                "selected_variant_card_mismatch",
                f"{label}: выбранная версия принадлежит другой карточке",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
                details={"variant_id": variant_id, "variant_card_id": variant_card_id},
            ))
            continue
        variant_field = str(variant.get("output_field", "") or "")
        if variant_field != output_field:
            reasons.append(_reason(
                "selected_variant_field_mismatch",
                f"{label}: выбранная версия относится к полю {variant_field or 'unknown'}",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
                details={
                    "variant_id": variant_id,
                    "expected_field": output_field,
                    "variant_field": variant_field,
                },
            ))
            continue

        expected_hash = str(variant.get("file_hash", "") or "").strip().lower()
        if len(expected_hash) != 64 or any(ch not in "0123456789abcdef" for ch in expected_hash):
            reasons.append(_reason(
                "selected_variant_hash_missing",
                f"{label}: у выбранной версии отсутствует корректный SHA-256",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
                details={"variant_id": variant_id},
            ))
            continue

        variant_path = Path(str(variant.get("file_path", "") or ""))
        if not variant_path.is_absolute():
            variant_path = project_dir / variant_path
        try:
            variant_path = variant_path.resolve()
        except OSError:
            pass
        variant_root = project_dir / "assets" / "variants"
        if not is_within(variant_path, variant_root):
            reasons.append(_reason(
                "selected_variant_outside_store",
                f"{label}: файл выбранной версии находится вне immutable-хранилища",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
                details={"variant_id": variant_id},
            ))
            continue
        if not variant_path.is_file():
            reasons.append(_reason(
                "selected_variant_media_missing",
                f"{label}: неизменяемый файл выбранной версии отсутствует",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
                details={"variant_id": variant_id},
            ))
            continue
        try:
            snapshot_hash = content_hash(variant_path)
            current_hash = content_hash(media_path)
        except OSError as exc:
            reasons.append(_reason(
                "provenance_media_unreadable",
                f"{label}: не удалось проверить байты медиа",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
                details={"variant_id": variant_id, "error": str(exc)},
            ))
            continue
        if snapshot_hash != expected_hash:
            reasons.append(_reason(
                "selected_variant_corrupt",
                f"{label}: файл выбранной версии не совпадает с её SHA-256",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
                details={
                    "variant_id": variant_id,
                    "expected_sha256": expected_hash,
                    "actual_sha256": snapshot_hash,
                },
            ))
            continue
        if current_hash != expected_hash:
            reasons.append(_reason(
                "selected_media_mismatch",
                f"{label}: экспортируемый файл не совпадает с выбранной версией",
                card_index=card_index,
                shot_index=shot_index,
                card_id=card_id,
                details={
                    "variant_id": variant_id,
                    "output_field": output_field,
                    "expected_sha256": expected_hash,
                    "actual_sha256": current_hash,
                },
            ))
            continue

        raw_metadata = variant.get("metadata")
        variant_metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        rights = {
            "source_url": str(variant_metadata.get("source_url") or "").strip(),
            "license": str(variant_metadata.get("source_license") or "").strip(),
            "author": str(variant_metadata.get("source_author") or "").strip(),
            "reference_url": str(variant_metadata.get("reference_url") or "").strip(),
            "reference_source": str(variant_metadata.get("reference_source") or "").strip(),
            "required_credit": str(variant_metadata.get("reference_credit") or "").strip(),
        }
        rights = {key: value for key, value in rights.items() if value}
        immutable_source = str(variant.get("source", "") or "").strip().lower()
        immutable_mode = str(variant.get("mode", "") or "").strip().lower()
        if route == "stock" or immutable_source == "stock" or immutable_mode == "stock":
            missing_rights = False
            if not rights.get("source_url"):
                reasons.append(_reason(
                    "missing_stock_source_url",
                    f"{label}: для стокового медиа не зафиксирован source URL",
                    card_index=card_index,
                    shot_index=shot_index,
                    card_id=card_id,
                    details={"variant_id": variant_id},
                ))
                missing_rights = True
            if not rights.get("license"):
                reasons.append(_reason(
                    "missing_stock_license",
                    f"{label}: для стокового медиа не зафиксирована лицензия",
                    card_index=card_index,
                    shot_index=shot_index,
                    card_id=card_id,
                    details={"variant_id": variant_id},
                ))
                missing_rights = True
            if missing_rights:
                continue

        records.append({
            "card_id": card_id,
            "card_index": card_index,
            "shot_index": shot_index,
            "route": route,
            "output_field": output_field,
            "media_path": _portable_path(media_path, project_dir),
            "media_sha256": current_hash,
            "media_size": media_path.stat().st_size,
            "variant": {
                "id": variant_id,
                "created_at": variant.get("created_at"),
                "source": variant.get("source"),
                "mode": variant.get("mode"),
                "file_path": _portable_path(variant_path, project_dir),
                "file_sha256": expected_hash,
                "file_size": _integer(variant.get("file_size")) or variant_path.stat().st_size,
                "model_hash": str(variant.get("model_hash", "") or ""),
                "settings_hash": str(variant.get("settings_hash", "") or ""),
                "workflow_hash": str(variant.get("workflow_hash", "") or ""),
                "rights": rights,
            },
        })

    return {
        "ok": not reasons,
        "schema_version": MASTER_MANIFEST_SCHEMA_VERSION,
        "variant_manifest": manifest_contract,
        "cards": records,
        "reasons": reasons,
    }


def _redact_secrets(value: Any) -> Any:
    sensitive = ("api_key", "apikey", "secret", "token", "password", "credential")
    if isinstance(value, dict):
        return {
            str(key): _redact_secrets(item)
            for key, item in value.items()
            if not any(marker in str(key).lower() for marker in sensitive)
        }
    if isinstance(value, (list, tuple)):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def settings_fingerprint(settings: dict[str, Any]) -> str:
    """Hash a secret-free rendering contract deterministically."""
    return content_hash(_redact_secrets(settings))


def _input_file_contract(path: Path | None, project_dir: Path) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    resolved = path.resolve()
    return {
        "path": _portable_path(resolved, project_dir),
        "sha256": content_hash(resolved),
        "size": resolved.stat().st_size,
    }


def build_edit_contract(
    project_dir: Path | str,
    cards: Iterable[Any],
    *,
    timeline_settings: dict[str, Any] | None = None,
    project_metadata: dict[str, Any] | None = None,
    settings_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze the edit decisions and actual audio inputs consumed by FFmpeg."""
    project_dir = Path(project_dir).resolve()
    metadata = dict(project_metadata or {})
    settings = dict(settings_contract or {})
    timeline = dict(timeline_settings or {})
    card_contracts: list[dict[str, Any]] = []
    sfx_inputs: list[dict[str, Any]] = []

    for card_index, card in enumerate(cards):
        status = str(_get(card, "status", "") or "").strip().lower()
        if status in EXCLUDED_STATUSES:
            continue
        card_contracts.append({
            "card_id": str(_get(card, "card_id", "") or ""),
            "card_index": card_index,
            "shot_index": _integer(_get(card, "shot_index", None)),
            "scene_index": _integer(_get(card, "scene_index", None)),
            "route": card_route(card),
            "start": str(_get(card, "start", "") or ""),
            "end": str(_get(card, "end", "") or ""),
            "timeline_start": str(_get(card, "timeline_start", "") or ""),
            "timeline_end": str(_get(card, "timeline_end", "") or ""),
            "track": str(_get(card, "track", "") or ""),
            "text_overlay": {
                "enabled": bool(_get(card, "text_overlay_enabled", False)),
                "text": str(_get(card, "text_overlay_text", "") or ""),
                "position": str(_get(card, "text_overlay_position", "") or ""),
                "style": str(_get(card, "text_overlay_style", "") or ""),
                "x": _get(card, "text_overlay_x", 0),
                "y": _get(card, "text_overlay_y", 0),
                "enter": str(_get(card, "text_overlay_enter", "") or ""),
                "exit": str(_get(card, "text_overlay_exit", "") or ""),
            },
        })
        raw_sfx = str(_get(card, "sfx_output", "") or "").strip()
        if raw_sfx and bool(settings.get("sfx_enabled", True)):
            sfx_path = Path(raw_sfx)
            if not sfx_path.is_absolute():
                sfx_path = project_dir / sfx_path
            file_contract = _input_file_contract(sfx_path, project_dir)
            if file_contract:
                sfx_inputs.append({
                    "card_id": str(_get(card, "card_id", "") or ""),
                    "shot_index": _integer(_get(card, "shot_index", None)),
                    **file_contract,
                })

    voice = _input_file_contract(find_voice_audio(project_dir), project_dir)
    music_path = resolve_music_track(project_dir) if bool(metadata.get("music_enabled", True)) else None
    music = _input_file_contract(music_path, project_dir)
    music_trim: dict[str, Any] | None = None
    if music_path is not None:
        tracks_path = project_dir / "music" / "tracks.json"
        if tracks_path.is_file():
            try:
                raw_tracks = json.loads(tracks_path.read_text(encoding="utf-8"))
                selected_trim = raw_tracks.get(music_path.name, {}) if isinstance(raw_tracks, dict) else {}
                if isinstance(selected_trim, dict):
                    music_trim = {
                        "trim_start": selected_trim.get("trim_start", 0),
                        "trim_end": selected_trim.get("trim_end", 0),
                    }
            except (OSError, json.JSONDecodeError):
                music_trim = None

    return {
        "schema_version": 1,
        "timeline": timeline,
        "cards": card_contracts,
        "audio": {
            "voice": voice,
            "music": music,
            "music_trim": music_trim,
            "music_settings": {
                key: metadata.get(key)
                for key in ("music_enabled", "music_volume_db", "music_duck", "music_selected")
            },
            "sfx": sfx_inputs,
        },
    }


def _application_version() -> str:
    return __version__


def tool_contract(ffmpeg: str = "ffmpeg") -> dict[str, Any]:
    """Capture locally available renderer versions without making them mandatory."""
    resolved = resolve_executable(ffmpeg, "ffmpeg.exe", "ffmpeg")
    ffmpeg_version = ""
    try:
        completed = subprocess.run(
            [resolved, "-version"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        ffmpeg_version = (completed.stdout or completed.stderr or "").splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "application": "pipeline-studio",
        "application_version": _application_version(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "ffmpeg": {
            "configured": Path(str(ffmpeg)).name,
            "resolved": Path(str(resolved)).name,
            "version": ffmpeg_version,
        },
    }


def master_sidecar_path(output_path: Path | str) -> Path:
    output = Path(output_path)
    return output.with_suffix(output.suffix + ".manifest.json")


def _write_json_create_only(path: Path, payload: dict[str, Any]) -> None:
    """Publish complete JSON atomically and fail if the destination exists.

    ``os.link`` is an atomic create-if-absent operation on the same filesystem,
    unlike ``os.replace`` which would violate the immutability contract.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ExportManifestExists(f"Master manifest already exists: {path}")
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp_name, path)
        except FileExistsError as exc:
            raise ExportManifestExists(f"Master manifest already exists: {path}") from exc
        except OSError as exc:
            # Failing closed is preferable to a non-atomic fallback that could
            # overwrite an immutable audit record in a race.
            raise ExportManifestError(
                f"Filesystem cannot atomically publish Master manifest: {path}"
            ) from exc
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def load_master_export_index(exports_dir: Path | str) -> list[dict[str, Any]]:
    """Load and verify the append history that anchors sidecar hashes."""
    path = Path(exports_dir) / MASTER_EXPORT_INDEX_FILE
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportManifestError("Master export integrity index is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or int(payload.get("schema_version", 0) or 0) != MASTER_EXPORT_INDEX_SCHEMA_VERSION
        or not isinstance(payload.get("records"), list)
    ):
        raise ExportManifestError("Master export integrity index has invalid structure")
    verified: list[dict[str, Any]] = []
    previous_hash = ""
    seen_sidecars: set[str] = set()
    for raw in payload["records"]:
        if not isinstance(raw, dict):
            raise ExportManifestError("Master export integrity index has invalid record")
        record = dict(raw)
        record_hash = str(record.pop("record_hash", "") or "")
        sidecar = str(record.get("sidecar", "") or "")
        if (
            not sidecar
            or Path(sidecar).name != sidecar
            or sidecar in seen_sidecars
            or str(record.get("previous_record_hash", "") or "") != previous_hash
            or content_hash(record) != record_hash
        ):
            raise ExportManifestError("Master export integrity index chain is invalid")
        seen_sidecars.add(sidecar)
        previous_hash = record_hash
        verified.append({**record, "record_hash": record_hash})
    return verified


def _append_master_export_index(
    sidecar: Path,
    payload: dict[str, Any],
    sidecar_sha256: str,
) -> None:
    exports_dir = sidecar.parent
    records = load_master_export_index(exports_dir)
    if any(record.get("sidecar") == sidecar.name for record in records):
        raise ExportManifestExists(f"Master manifest already indexed: {sidecar.name}")
    previous_hash = str(records[-1].get("record_hash", "") or "") if records else ""
    export_payload = payload["export"]
    record = {
        "sidecar": sidecar.name,
        "sidecar_sha256": sidecar_sha256,
        "sidecar_size": sidecar.stat().st_size,
        "manifest_id": payload["manifest_id"],
        "created_at": payload["created_at"],
        "output": export_payload["path"],
        "output_sha256": export_payload["sha256"],
        "output_size": export_payload["size"],
        "previous_record_hash": previous_hash,
    }
    record["record_hash"] = content_hash(record)
    stored_records = [dict(item) for item in records]
    stored_records.append(record)
    atomic_write_text(
        exports_dir / MASTER_EXPORT_INDEX_FILE,
        json.dumps(
            {
                "schema_version": MASTER_EXPORT_INDEX_SCHEMA_VERSION,
                "records": stored_records,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    )


def write_master_export_manifest(
    output_path: Path | str,
    *,
    project_id: str,
    project_name: str,
    project_schema_version: int,
    provenance: dict[str, Any],
    profile: dict[str, Any],
    settings_contract: dict[str, Any],
    technical_qc: dict[str, Any],
    edit_contract: dict[str, Any] | None = None,
    ffmpeg: str = "ffmpeg",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Write and return metadata for an immutable successful-Master sidecar."""
    output = Path(output_path).resolve()
    if not output.is_file():
        raise FileNotFoundError(output)
    if not provenance.get("ok"):
        raise ExportManifestError("Cannot write a Master manifest for invalid provenance")

    created = created_at or _utc_now()
    clean_settings = _redact_secrets(dict(settings_contract))
    profile_contract = dict(profile)
    clean_edit_contract = _redact_secrets(dict(edit_contract or {}))
    payload = {
        "schema_version": MASTER_MANIFEST_SCHEMA_VERSION,
        "manifest_type": MASTER_MANIFEST_TYPE,
        "manifest_id": "master_" + uuid.uuid4().hex,
        "created_at": created,
        "project": {
            "id": str(project_id),
            "name": str(project_name),
            "schema_version": int(project_schema_version),
        },
        "export": {
            "mode": "master",
            "path": output.name,
            "sha256": content_hash(output),
            "size": output.stat().st_size,
            "profile": profile_contract,
            "settings": clean_settings,
            "settings_fingerprint": settings_fingerprint(clean_settings),
            "contract_fingerprint": content_hash({
                "profile": profile_contract,
                "settings": clean_settings,
                "edit_contract": clean_edit_contract,
                "provenance": provenance.get("cards", []),
            }),
            "edit_contract": clean_edit_contract,
            "technical_qc": technical_qc,
        },
        "provenance": {
            "variant_manifest": provenance.get("variant_manifest"),
            "cards": list(provenance.get("cards", [])),
        },
        "tools": tool_contract(ffmpeg),
    }
    sidecar = master_sidecar_path(output)
    _write_json_create_only(sidecar, payload)
    sidecar_sha256 = content_hash(sidecar)
    _append_master_export_index(sidecar, payload, sidecar_sha256)
    return {
        "path": sidecar.name,
        "sha256": sidecar_sha256,
        "size": sidecar.stat().st_size,
        "manifest_id": payload["manifest_id"],
        "created_at": created,
        "output_sha256": payload["export"]["sha256"],
    }


__all__ = [
    "ExportManifestError",
    "ExportManifestExists",
    "MASTER_MANIFEST_SCHEMA_VERSION",
    "MASTER_EXPORT_INDEX_FILE",
    "chosen_card_media",
    "build_edit_contract",
    "evaluate_master_provenance",
    "master_sidecar_path",
    "load_master_export_index",
    "settings_fingerprint",
    "tool_contract",
    "write_master_export_manifest",
]
