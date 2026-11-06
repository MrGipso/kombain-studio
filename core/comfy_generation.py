"""UI-independent workflow preparation and media generation for ComfyUI.

This module is the application layer above :mod:`core.comfy`: it prepares the
repository workflows for one asset and stores the generated output in the
project.  It deliberately has no UI dependency so web workers can import it
in headless environments.

Which node receives the prompt, the input image, the duration and so on is
decided by the marker contract in :mod:`core.comfy_contract` (``ENGINE_*``
markers in node titles, with a fallback to the bundled node ids), so any
workflow can replace a bundled one without code changes.

Callers that already opened a project should pass its ``Settings`` snapshot.
That keeps workflow selection, LTX draft/resolution and FFmpeg normalization
stable for the complete job.  When the argument is omitted the current global
settings are used.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from core import comfy_contract, continuity
from core.comfy import (
    DEFAULT_WORKFLOW_PATHS,
    comfy_collect_outputs,
    comfy_download_file,
    comfy_download_output,
    comfy_queue_prompt,
    comfy_upload_image,
    comfy_wait_for_history,
)
from core.config import load_settings
from core.models import AssetCard, timecode_to_seconds
from core.style_presets import get_video_style
from core.transcribe import find_default_tool


DEFAULT_COMFY_KREA_WORKFLOW = DEFAULT_WORKFLOW_PATHS["krea"]
DEFAULT_COMFY_IDEOGRAM_WORKFLOW = DEFAULT_WORKFLOW_PATHS["ideogram"]
DEFAULT_COMFY_LTX_WORKFLOW = DEFAULT_WORKFLOW_PATHS["ltx"]
DEFAULT_COMFY_LTX_DRAFT_WORKFLOW = DEFAULT_WORKFLOW_PATHS["ltx_draft"]
DEFAULT_COMFY_ACE_WORKFLOW = DEFAULT_WORKFLOW_PATHS["music"]

MAX_LTX_DURATION = 15
MAX_ACE_DURATION = 120
LTX_RESOLUTIONS = {"1920x1088": (1920, 1088), "1280x704": (1280, 704)}
REFERENCE_NODE_TITLE = comfy_contract.REFERENCE_IMAGE
REFERENCE_PROMPT_TITLE = comfy_contract.PROMPT
REFERENCE_OUTPUT_TITLE = comfy_contract.OUTPUT
# Seeds stay below 2**31: several ComfyUI cloud API nodes declare a signed
# 32-bit seed input and reject larger values during prompt validation.
_SEED_MODULUS = 2_147_000_000


def _effective_settings(settings: object | None) -> object:
    return settings if settings is not None else load_settings()


def _setting(settings: object, key: str, default: Any = "") -> Any:
    if isinstance(settings, dict):
        return settings.get(key, default)
    return getattr(settings, key, default)


def comfy_api_key(settings: object | None = None) -> str:
    """The comfy.org key for partner/API nodes, or ``""`` when not configured."""

    return str(_setting(_effective_settings(settings), "comfy_api_key", "") or "").strip()


def _queue_prompt(base_url: str, workflow: dict, settings: object | None) -> str:
    """Queue ``workflow`` with the snapshot's comfy.org key, if one is set.

    Without a key this is the plain two-argument call, so local-only setups
    send the same payload as before the key existed.
    """

    api_key = comfy_api_key(settings)
    if api_key:
        return comfy_queue_prompt(base_url, workflow, api_key=api_key)
    return comfy_queue_prompt(base_url, workflow)


def set_workflow_seed_values(
    workflow: dict,
    targets: tuple[comfy_contract.Binding, ...] | None = None,
) -> None:
    """Assign fresh seeds to ``targets`` or to every supported seed input."""

    seed = int(time.time() * 1000) % _SEED_MODULUS
    if targets is not None:
        for target in targets:
            workflow[target.node_id]["inputs"][target.key] = seed
            seed += 1
        return
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        for key in list(inputs):
            if key in {"seed", "noise_seed", "sampling_mode.seed"}:
                inputs[key] = seed
                seed += 1


def configured_workflow_path(
    setting_key: str,
    default_path: Path,
    *,
    settings: object | None = None,
) -> Path:
    """Resolve one workflow override from an immutable settings snapshot."""

    effective = _effective_settings(settings)
    configured = str(_setting(effective, setting_key, "") or "").strip()
    return Path(configured).expanduser() if configured else Path(default_path)


def _read_workflow(path: Path, *, label: str = "Workflow") -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    workflow = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(workflow, dict):
        raise ValueError(f"{label} root must be an object: {path}")
    return workflow


def _bind_workflow(
    workflow: dict,
    name: str,
    path: Path,
) -> comfy_contract.BoundWorkflow:
    try:
        return comfy_contract.bind_workflow(workflow, name)
    except comfy_contract.WorkflowContractError as exc:
        raise comfy_contract.WorkflowContractError(f"{exc} [{path}]") from None


def _apply_seeds(bound: comfy_contract.BoundWorkflow) -> None:
    """Honour ENGINE_SEED markers, else randomise every seed input."""

    targets = bound.targets(comfy_contract.SEED)
    set_workflow_seed_values(bound.workflow, targets or None)


def reference_workflow_status(settings: object | None = None) -> dict[str, Any]:
    """Validate the static reference-workflow contract without contacting ComfyUI."""

    effective = _effective_settings(settings)
    mode = str(
        _setting(effective, "reference_conditioning_mode", "auto") or "auto"
    ).strip().lower()
    configured = str(
        _setting(effective, "comfy_reference_workflow", "") or ""
    ).strip()
    result: dict[str, Any] = {
        "mode": mode,
        "configured": bool(configured),
        "enabled": False,
        "ok": True,
        "path": configured,
    }
    if mode == "off":
        return result
    if not configured:
        if mode == "required":
            result["ok"] = False
            result["error"] = "Reference workflow is required but not configured"
        return result
    path = Path(configured).expanduser()
    try:
        workflow = _read_workflow(path, label="Reference workflow")
        comfy_contract.bind_workflow(workflow, "reference")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result["ok"] = False
        result["error"] = str(exc)
        return result
    result["enabled"] = True
    result["path"] = str(path)
    result["workflow_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def prepare_reference_image_workflow(
    prompt: str,
    reference_image_name: str,
    shot_index: int,
    *,
    settings: object | None = None,
) -> dict:
    """Bind one canonical image to a marker-based API workflow."""

    status = reference_workflow_status(settings)
    if not status.get("ok") or not status.get("enabled"):
        raise RuntimeError(
            str(status.get("error") or "Reference workflow is not configured")
        )
    path = Path(str(status["path"]))
    workflow = _read_workflow(path, label="Reference workflow")
    bound = _bind_workflow(workflow, "reference", path)
    bound.set(comfy_contract.REFERENCE_IMAGE, reference_image_name)
    bound.set(comfy_contract.PROMPT, prompt)
    bound.set(
        comfy_contract.OUTPUT,
        f"content_pipeline/shot_{shot_index:03d}_reference",
    )
    _apply_seeds(bound)
    return workflow


def prepare_image_workflow(
    model: str,
    prompt: str,
    shot_index: int,
    *,
    settings: object | None = None,
) -> dict:
    if model == "ideogram4":
        workflow_path = configured_workflow_path(
            "comfy_ideogram_workflow",
            DEFAULT_COMFY_IDEOGRAM_WORKFLOW,
            settings=settings,
        )
    else:
        workflow_path = configured_workflow_path(
            "comfy_krea_workflow",
            DEFAULT_COMFY_KREA_WORKFLOW,
            settings=settings,
        )
        model = "krea2_turbo"
    workflow = _read_workflow(workflow_path)
    if model == "ideogram4":
        bound = _bind_workflow(workflow, "ideogram", workflow_path)
        suffix = "ideogram4"
    else:
        bound = _bind_workflow(workflow, "krea", workflow_path)
        suffix = "krea2"
    bound.set(comfy_contract.PROMPT, prompt)
    bound.set(
        comfy_contract.OUTPUT,
        f"content_pipeline/shot_{shot_index:03d}_{suffix}",
    )
    _apply_seeds(bound)
    return workflow


def ltx_render_resolution(settings: object | None = None) -> tuple[int, int]:
    """Return a supported LTX latent-grid render size."""

    effective = _effective_settings(settings)
    value = str(_setting(effective, "ltx_resolution", "1920x1088") or "").strip().lower()
    return LTX_RESOLUTIONS.get(value, LTX_RESOLUTIONS["1920x1088"])


def project_video_style_id(project_dir: Path) -> str:
    """Read the project's style id without constructing a GUI or Project."""

    meta_path = Path(project_dir) / ".project.json"
    if meta_path.exists():
        try:
            return str(
                json.loads(meta_path.read_text(encoding="utf-8")).get("video_style", "")
            ).strip()
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    return ""


def style_negative_for_clip(style_negative: str) -> str:
    """Convert a prose style negative into a comma-list for CLIP."""

    text = (style_negative or "").strip()
    if not text:
        return ""
    for prefix in ("Avoid ", "avoid ", "Also avoid: ", "also avoid: "):
        text = text.replace(prefix, "")
    text = text.replace(".", ",").replace("\n", " ")
    return ", ".join(part.strip() for part in text.split(",") if part.strip())


def ltx_draft_for_card(card: AssetCard, settings: object | None = None) -> bool:
    """Apply the configured off/all/auto LTX draft policy to one card."""

    effective = _effective_settings(settings)
    mode = str(_setting(effective, "ltx_draft_mode", "off") or "off").strip().lower()
    if mode == "all":
        return True
    if mode == "auto":
        return (getattr(card, "priority", "") or "medium").strip().lower() != "high"
    return False


def prepare_ltx_workflow(
    prompt: str,
    input_image_name: str,
    shot_index: int,
    duration_seconds: float = 10.0,
    draft: bool = False,
    negative_extra: str = "",
    *,
    settings: object | None = None,
) -> dict:
    if draft:
        workflow_path = configured_workflow_path(
            "comfy_ltx_draft_workflow",
            DEFAULT_COMFY_LTX_DRAFT_WORKFLOW,
            settings=settings,
        )
    else:
        workflow_path = configured_workflow_path(
            "comfy_ltx_workflow",
            DEFAULT_COMFY_LTX_WORKFLOW,
            settings=settings,
        )
    workflow = _read_workflow(workflow_path)
    bound = _bind_workflow(workflow, "ltx_draft" if draft else "ltx", workflow_path)
    bound.set(comfy_contract.INPUT_IMAGE, input_image_name)
    bound.set(comfy_contract.PROMPT, prompt)
    bound.set(
        comfy_contract.OUTPUT,
        f"content_pipeline/video/shot_{shot_index:03d}_ltx2_3",
    )
    # Without ENGINE_DURATION the clip length is whatever the workflow renders.
    bound.set(
        comfy_contract.DURATION,
        max(2.0, min(duration_seconds, MAX_LTX_DURATION)),
    )
    width, height = ltx_render_resolution(settings)
    bound.set(comfy_contract.WIDTH, width)
    bound.set(comfy_contract.HEIGHT, height)
    if negative_extra:

        def append_negative(current: str) -> str:
            base = current.strip().rstrip(",")
            return f"{base}, {negative_extra}" if base else negative_extra

        bound.transform(comfy_contract.NEGATIVE, append_negative)
    _apply_seeds(bound)
    return workflow


def generate_comfy_image(
    base_url: str,
    project_dir: Path,
    card: AssetCard,
    *,
    cancel_check: Callable[[], bool] | None = None,
    settings: object | None = None,
    reference_image: Path | None = None,
) -> Path:
    model = card.image_model or "krea2_turbo"
    prompt = continuity.compile_generation_prompt(
        card.image_prompt,
        card,
        target="image",
        context_bible=continuity.load_context_bible(project_dir),
    )
    if not prompt:
        raise ValueError("Image prompt is empty")
    reference_status = reference_workflow_status(settings)
    if reference_image is not None and reference_status.get("enabled"):
        # Validate the local workflow before mutating ComfyUI's input directory.
        _read_workflow(Path(str(reference_status["path"])), label="Reference workflow")
        digest = hashlib.sha256(Path(reference_image).read_bytes()).hexdigest()[:12]
        uploaded = comfy_upload_image(
            base_url,
            Path(reference_image),
            f"canonical_{digest}",
        )
        workflow = prepare_reference_image_workflow(
            prompt,
            uploaded,
            card.shot_index,
            settings=settings,
        )
        model = "reference"
    elif reference_image is not None and not reference_status.get("ok"):
        raise RuntimeError(
            str(reference_status.get("error") or "Reference workflow unavailable")
        )
    else:
        workflow = prepare_image_workflow(
            model,
            prompt,
            card.shot_index,
            settings=settings,
        )
    prompt_id = _queue_prompt(base_url, workflow, settings)
    history = comfy_wait_for_history(base_url, prompt_id, cancel_check=cancel_check)
    outputs = comfy_collect_outputs(history, ("images",))
    if not outputs:
        raise RuntimeError("ComfyUI finished, but no image output was found")
    prefix = model.replace(".", "_").replace("-", "_")
    return comfy_download_output(
        base_url,
        outputs[-1],
        Path(project_dir) / "assets" / "images",
        card.shot_index,
        prefix,
    )


def generate_comfy_ltx_video(
    base_url: str,
    project_dir: Path,
    card: AssetCard,
    draft: bool | None = None,
    *,
    cancel_check: Callable[[], bool] | None = None,
    settings: object | None = None,
) -> Path:
    if not card.image_output or not Path(card.image_output).exists():
        raise FileNotFoundError("Для LTX сначала нужна готовая картинка в image_output.")
    prompt = continuity.compile_generation_prompt(
        card.video_prompt.strip() or card.image_prompt.strip(),
        card,
        target="video",
        context_bible=continuity.load_context_bible(project_dir),
    )
    if not prompt:
        raise ValueError("Video prompt is empty")
    duration = max(
        2.0,
        timecode_to_seconds(card.end) - timecode_to_seconds(card.start),
    )
    duration = min(duration, MAX_LTX_DURATION)
    use_draft = ltx_draft_for_card(card, settings) if draft is None else bool(draft)
    card.ltx_draft = use_draft
    upload_name = comfy_upload_image(
        base_url,
        Path(card.image_output),
        f"shot_{card.shot_index:03d}_ltx_input",
    )
    style = get_video_style(project_video_style_id(Path(project_dir)))
    workflow = prepare_ltx_workflow(
        prompt,
        upload_name,
        card.shot_index,
        duration_seconds=duration,
        draft=use_draft,
        negative_extra=style_negative_for_clip(style.negative_prompt),
        settings=settings,
    )
    prompt_id = _queue_prompt(base_url, workflow, settings)
    history = comfy_wait_for_history(
        base_url,
        prompt_id,
        timeout_seconds=3600,
        cancel_check=cancel_check,
    )
    outputs = comfy_collect_outputs(history, ("videos", "gifs", "images"))
    if not outputs:
        raise RuntimeError("ComfyUI finished, but no video output was found")
    return comfy_download_output(
        base_url,
        outputs[-1],
        Path(project_dir) / "assets" / "videos",
        card.shot_index,
        "ltx2_3",
    )


def prepare_music_workflow(
    tags: str,
    bpm: int,
    seconds: int,
    lyrics: str = "",
    *,
    settings: object | None = None,
) -> dict:
    """Load ACE-Step and inject an instrumental background-music brief."""

    workflow_path = configured_workflow_path(
        "comfy_ace_workflow",
        DEFAULT_COMFY_ACE_WORKFLOW,
        settings=settings,
    )
    workflow = _read_workflow(workflow_path, label="Music workflow")
    bound = _bind_workflow(workflow, "music", workflow_path)
    seconds = int(max(15, min(seconds, MAX_ACE_DURATION)))
    bound.set(
        comfy_contract.PROMPT,
        tags.strip() or "cinematic ambient background, soft, unobtrusive",
    )
    bound.set(comfy_contract.LYRICS, lyrics.strip() or "[instrumental]")
    if bpm:
        bound.set(comfy_contract.BPM, int(bpm))
    bound.set(comfy_contract.DURATION, seconds)
    bound.set(comfy_contract.OUTPUT, "content_pipeline/music/track")
    if bound.mode == comfy_contract.MODE_LEGACY:
        # Unmarked copies of the original ACE-Step file carry 16 steps; the
        # bundled turbo checkpoint is tuned for 8.  A marked workflow is
        # trusted to declare its own sampler settings.
        ksampler = workflow.get("3", {}).get("inputs")
        if isinstance(ksampler, dict) and "steps" in ksampler:
            ksampler["steps"] = 8
    _apply_seeds(bound)
    return workflow


def _find_ffmpeg_path(settings: object | None) -> str:
    effective = _effective_settings(settings)
    configured = str(_setting(effective, "ffmpeg", "") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.exists():
            return str(candidate)
        if candidate.parent == Path("."):
            return configured
    return (
        shutil.which("ffmpeg.exe")
        or shutil.which("ffmpeg")
        or find_default_tool("ffmpeg.exe")
        or "ffmpeg"
    )


def _limit_music_peak(target: Path, settings: object | None) -> None:
    """Apply the legacy ACE-Step -1 dBFS peak limiter, best effort."""

    limited = target.with_name(target.stem + "_lim" + target.suffix)
    try:
        proc = subprocess.run(
            [
                _find_ffmpeg_path(settings),
                "-y",
                "-i",
                str(target),
                "-af",
                "alimiter=limit=0.891:level=false",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(limited),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode == 0 and limited.exists() and limited.stat().st_size > 0:
            os.replace(limited, target)
    except Exception:
        pass
    finally:
        # A failed encoder must never leave a second partial track in the music
        # library.  The raw Comfy output remains available as the safe fallback.
        limited.unlink(missing_ok=True)


def generate_comfy_music(
    base_url: str,
    project_dir: Path,
    tags: str,
    bpm: int = 90,
    seconds: int = 90,
    lyrics: str = "",
    *,
    cancel_check: Callable[[], bool] | None = None,
    settings: object | None = None,
) -> Path:
    """Generate and store a uniquely named ACE-Step background music bed."""

    workflow = prepare_music_workflow(
        tags,
        bpm,
        seconds,
        lyrics,
        settings=settings,
    )
    prompt_id = _queue_prompt(base_url, workflow, settings)
    history = comfy_wait_for_history(
        base_url,
        prompt_id,
        timeout_seconds=1800,
        cancel_check=cancel_check,
    )
    outputs = comfy_collect_outputs(history, ("audio",))
    if not outputs:
        raise RuntimeError("ComfyUI finished, but no audio output was found")
    music_dir = Path(project_dir) / "music"
    music_dir.mkdir(parents=True, exist_ok=True)
    item = outputs[-1]
    filename = item.get("filename", "")
    if not filename:
        raise RuntimeError(f"ComfyUI audio output has no filename: {item}")
    ext = Path(filename).suffix or ".mp3"
    target = music_dir / (
        f"track_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
    )
    comfy_download_file(base_url, item, target)
    _limit_music_peak(target, settings)
    return target


__all__ = [
    "DEFAULT_COMFY_ACE_WORKFLOW",
    "DEFAULT_COMFY_IDEOGRAM_WORKFLOW",
    "DEFAULT_COMFY_KREA_WORKFLOW",
    "DEFAULT_COMFY_LTX_DRAFT_WORKFLOW",
    "DEFAULT_COMFY_LTX_WORKFLOW",
    "LTX_RESOLUTIONS",
    "MAX_ACE_DURATION",
    "MAX_LTX_DURATION",
    "REFERENCE_NODE_TITLE",
    "REFERENCE_OUTPUT_TITLE",
    "REFERENCE_PROMPT_TITLE",
    "comfy_api_key",
    "configured_workflow_path",
    "generate_comfy_image",
    "generate_comfy_ltx_video",
    "generate_comfy_music",
    "ltx_draft_for_card",
    "ltx_render_resolution",
    "prepare_image_workflow",
    "prepare_ltx_workflow",
    "prepare_music_workflow",
    "prepare_reference_image_workflow",
    "project_video_style_id",
    "reference_workflow_status",
    "set_workflow_seed_values",
    "style_negative_for_clip",
]
