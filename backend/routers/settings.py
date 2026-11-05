from __future__ import annotations

from pathlib import Path
import ipaddress
from typing import Any
from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import TypeAdapter, ValidationError

from core.config import Settings, load_settings, save_settings, list_presets
from core.project import is_within, list_projects, open_project
from backend.services import jobs, runtime_locks
from core.instance_lock import InstanceAlreadyRunning

router = APIRouter()

SECRET_KEYS = {"lm_api_key", "comfy_api_key", "pexels_api_key", "pixabay_api_key"}
READ_ONLY_RESPONSE_KEYS = {f"{key}_set" for key in SECRET_KEYS}

# Binary-path fields are the settings→execution chain (export/diagnostics run
# them). Restrict what a network request may point them at: the basename must
# match the expected tool, so PUT /settings can't stage an arbitrary executable
# for the next export/diagnostic call (audit issue #3).
BINARY_KEYS: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("ffmpeg", "ffmpeg.exe"),
    "whisper_cli": ("whisper-cli", "whisper-cli.exe", "main", "main.exe"),
    "omnivoice_cli": ("omnivoice-infer", "omnivoice-infer.exe"),
}


def _validate_binary_path(key: str, value: Any, output_base: Path | str) -> None:
    text = str(value or "").strip()
    if not text:
        return  # empty = auto-discover, always allowed
    allowed = BINARY_KEYS[key]
    path = Path(text).expanduser()
    if path.name.lower() not in allowed:
        raise HTTPException(
            400,
            f"Недопустимый путь для {key}: ожидается бинарник {allowed[0]}",
        )
    # A bare executable name is resolved through the server's trusted PATH.
    # Relative paths with directories depend on cwd and are therefore rejected.
    if path.parent == Path(".") and not path.is_absolute():
        return
    if not path.is_absolute():
        raise HTTPException(400, f"Путь для {key} должен быть абсолютным или именем из PATH")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, ValueError):
        raise HTTPException(400, f"Файл для {key} не найден")
    if not resolved.is_file():
        raise HTTPException(400, f"Путь для {key} не является файлом")
    if is_within(resolved, Path(output_base or "projects")):
        raise HTTPException(400, f"Путь для {key} не может находиться внутри каталога проектов")


def _validate_comfy_install_dir(value: Any, output_base: Path | str) -> None:
    text = str(value or "").strip()
    if not text:
        return
    root = Path(text).expanduser()
    if not root.is_absolute():
        raise HTTPException(400, "Каталог portable ComfyUI должен быть абсолютным")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, ValueError):
        raise HTTPException(400, "Каталог portable ComfyUI не найден")
    if not resolved.is_dir():
        raise HTTPException(400, "Путь portable ComfyUI не является каталогом")
    if is_within(resolved, Path(output_base or "projects")):
        raise HTTPException(
            400,
            "Portable ComfyUI не может находиться внутри каталога проектов",
        )
    required = (
        resolved / "python_embeded" / "python.exe",
        resolved / "ComfyUI" / "main.py",
    )
    if not all(path.is_file() for path in required):
        raise HTTPException(
            400,
            "В каталоге не найдены python_embeded/python.exe и ComfyUI/main.py",
        )


def _validate_comfy_listen_address(value: Any) -> None:
    text = str(value or "127.0.0.1").strip()
    try:
        ipaddress.ip_address(text)
    except ValueError:
        raise HTTPException(
            400,
            "Адрес ComfyUI --listen должен быть одним IP-адресом",
        )


@router.get("/settings")
async def get_settings():
    s = load_settings()
    data = {}
    for f in s.__dataclass_fields__.values():
        value = getattr(s, f.name)
        if f.name in SECRET_KEYS:
            data[f.name] = ""
            data[f"{f.name}_set"] = bool(value)
        else:
            data[f.name] = value
    return data


@router.put("/settings")
async def update_settings(data: dict[str, Any]):
    try:
        # Serializing before load+merge is essential: blank secret fields keep
        # the currently stored value, so validation against a stale snapshot
        # would otherwise be a lost-update race.
        with jobs.settings_mutation():
            current = load_settings()
            valid_keys = {f.name for f in current.__dataclass_fields__.values()}
            unknown = sorted(set(data) - valid_keys - READ_ONLY_RESPONSE_KEYS)
            if unknown:
                raise HTTPException(
                    422,
                    {"message": "Unknown settings fields", "fields": unknown},
                )

            candidate = asdict(current)
            for key, value in data.items():
                if key in valid_keys:
                    if key in SECRET_KEYS and (value is None or value == ""):
                        continue
                    candidate[key] = value
            for key in BINARY_KEYS:
                _validate_binary_path(
                    key,
                    candidate.get(key),
                    candidate.get("output_base", ""),
                )
            _validate_comfy_install_dir(
                candidate.get("comfy_install_dir"),
                candidate.get("output_base", ""),
            )
            _validate_comfy_listen_address(
                candidate.get("comfy_watchdog_listen_address"),
            )
            try:
                validated = TypeAdapter(Settings).validate_python(candidate)
            except ValidationError as exc:
                raise HTTPException(422, exc.errors())

            changed = {
                key
                for key in valid_keys
                if getattr(current, key) != getattr(validated, key)
            }
            # If output_base itself changes, jobs may still be running in the
            # old root while the new root can already contain projects. Lock
            # and invalidate the union before publishing the global setting.
            roots = {
                Path(current.output_base or "projects").resolve(),
                Path(validated.output_base or "projects").resolve(),
            }
            project_paths_by_key: dict[str, Path] = {}
            for root in roots:
                for discovered in list_projects(root):
                    key = str(discovered.dir.resolve()).casefold()
                    project_paths_by_key[key] = discovered.dir
            project_paths = list(project_paths_by_key.values())

            # Do not write the global file until every real project is locked.
            # Re-open inside the lock so invalidation never acts on the stale
            # Project instances used only for discovery above.
            with jobs.bulk_project_mutation(
                [path.name for path in project_paths],
                "Global settings update",
            ):
                projects = [
                    project
                    for path in project_paths
                    if (project := open_project(path, settings=validated)) is not None
                ]
                with runtime_locks.output_base_transition(
                    Path(validated.output_base or "projects")
                ):
                    save_settings(validated)

                transcript_inputs = {
                    "whisper_cli", "whisper_model", "whisper_language", "ffmpeg"
                }
                scene_inputs = {"lm_url", "lm_model", "lm_embed_model", "lm_context"}
                shot_inputs = {
                    "image_model_preference", "reference_enable", "reference_density",
                    "stock_variable_rhythm",
                }
                # comfy_api_key is deliberately absent: it authenticates cloud
                # nodes but never changes what a workflow renders.
                asset_inputs = {
                    "comfy_url", "comfy_krea_workflow", "comfy_reference_workflow",
                    "reference_conditioning_mode", "comfy_ideogram_workflow",
                    "comfy_ltx_workflow", "comfy_ltx_draft_workflow",
                    "pexels_api_key", "pixabay_api_key", "stock_score_threshold",
                    "stock_floor", "stock_retry_queries", "stock_max_seconds",
                    "stock_candidates", "stock_uniquify", "stock_mirror",
                    "stock_free_sources", "lm_vision_model",
                    "kenburns_variety", "ltx_draft_mode",
                    "ltx_resolution",
                }
                timeline_inputs = {
                    "comfy_ace_workflow", "sfx_enabled", "woosh_url", "sfx_volume_db",
                }
                export_inputs = {
                    "export_loudnorm", "music_normalize", "export_unify_grade",
                    "export_grain", "kenburns_variety", "ltx_resolution",
                }
                if changed:
                    for project in projects:
                        if changed & transcript_inputs:
                            project.invalidate_stage(
                                "transcript",
                                reason="global transcription settings changed",
                            )
                        if changed & scene_inputs:
                            project.invalidate_stage(
                                "scenes", reason="global LM settings changed"
                            )
                        if changed & shot_inputs:
                            project.invalidate_stage(
                                "shots", reason="global shot settings changed"
                            )
                        if changed & asset_inputs:
                            project.invalidate_stage(
                                "assets", reason="global asset settings changed"
                            )
                        if changed & timeline_inputs:
                            project.invalidate_stage(
                                "timeline", reason="global audio settings changed"
                            )
                        if changed & export_inputs:
                            project.invalidate_stage(
                                "export", reason="global export settings changed"
                            )
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except InstanceAlreadyRunning as exc:
        raise HTTPException(
            409,
            "Новый каталог проектов уже используется другим экземпляром Комбайн Studio",
        ) from exc
    return {"ok": True}


@router.get("/presets")
async def get_presets():
    presets = list_presets()
    return [
        {
            "name": p.name,
            "path": str(p.path),
            "info": p.raw.get("info", {}),
            "routes": p.routes,
        }
        for p in presets
    ]
