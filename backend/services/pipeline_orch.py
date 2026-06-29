from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import content_pipeline_gui as pipeline

from core.models import AssetCard as CoreAssetCard, Scene as CoreScene
from core.project import Project
from core.config import load_settings, save_settings, Settings
from core.export import export_video
from core.validation import write_validation_report

from backend.models.schemas import AssetCard


def _to_card(c: Any) -> dict[str, Any]:
    d = c.__dict__ if hasattr(c, "__dict__") else {}
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _to_cards(cards: list) -> list[dict[str, Any]]:
    return [_to_card(c) for c in cards]


def run_transcription(
    project_dir: Path,
    audio_path: Path,
    settings: dict[str, Any],
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    whisper_cli = Path(settings.get("whisper_cli", ""))
    model_path = Path(settings.get("whisper_model", ""))
    ffmpeg_path = Path(settings.get("ffmpeg", ""))
    language = settings.get("whisper_language", "ru")

    if not whisper_cli.exists():
        whisper_cli = pipeline.find_default_tool("whisper-cli.exe") or whisper_cli
    if not model_path.exists():
        mp = pipeline.find_default_model()
        model_path = Path(mp) if mp else model_path
    if not ffmpeg_path.exists():
        ffmpeg_path = pipeline.find_default_tool("ffmpeg.exe") or ffmpeg_path

    transcript_text, segments, json_path, srt_path = pipeline.transcribe_with_whisper_cpp(
        audio_path=audio_path,
        whisper_cli=Path(whisper_cli) if isinstance(whisper_cli, str) else whisper_cli,
        model_path=Path(model_path) if isinstance(model_path, str) else model_path,
        ffmpeg_path=Path(ffmpeg_path) if isinstance(ffmpeg_path, str) else ffmpeg_path,
        language=language,
        output_dir=project_dir / "transcription",
        log_callback=log,
    )

    voice_dir = project_dir / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    voice_target = voice_dir / audio_path.name
    if audio_path.exists() and audio_path.resolve() != voice_target.resolve():
        shutil.copy2(audio_path, voice_target)
        log(f"Voice audio saved: {voice_target}")

    core_seg_data = [{"start": s.start, "end": s.end, "text": s.text} for s in segments]

    return {
        "text": transcript_text,
        "segments": core_seg_data,
        "json_path": str(json_path) if json_path else "",
        "srt_path": str(srt_path) if srt_path else "",
    }


def _lm_unload_delay_seconds(settings: dict) -> int | None:
    if not settings.get("lm_auto_unload", True):
        return None
    try:
        return max(0, int(str(settings.get("lm_unload_delay", "0")).strip() or "0"))
    except ValueError:
        return 60


def _ensure_lm_context(settings: dict, log: Callable[[str], None]):
    base_url = settings.get("lm_url", "http://127.0.0.1:1234/v1")
    model = str(settings.get("lm_model", "local-model")).strip()
    ctx_raw = settings.get("lm_context", "32768")
    try:
        context_length = int(str(ctx_raw).strip())
    except ValueError:
        context_length = 32768
    if not model or model == "local-model":
        log("LM Studio context set skipped: no model name configured")
        return
    try:
        log(f"Setting LM Studio context to {context_length} for model {model}...")
        result = pipeline.load_lm_studio_model(base_url, model, context_length)
        log(f"LM Studio context updated: {json.dumps(result, ensure_ascii=False)}")
    except Exception as exc:
        log(f"LM Studio context warning (non-fatal): {pipeline.explain_lm_studio_error(exc)}")


def _maybe_unload_lm(settings: dict, reason: str, log: Callable[[str], None]):
    delay = _lm_unload_delay_seconds(settings)
    if delay is None:
        log("LM Studio auto-unload disabled")
        return
    if delay > 0:
        log(f"LM Studio TTL set ({delay}s) — model stays loaded")
        return
    base_url = settings.get("lm_url", "http://127.0.0.1:1234/v1")
    model = str(settings.get("lm_model", "local-model")).strip()
    if not model:
        log("LM Studio unload skipped: empty model id")
        return
    try:
        log(f"LM Studio unload: model={model}, reason={reason}")
        result = pipeline.unload_lm_studio_model(base_url, model)
        log(f"LM Studio unload result: {json.dumps(result, ensure_ascii=False)}")
    except Exception as exc:
        log(f"LM Studio unload error: {pipeline.explain_lm_studio_error(exc)}")


def run_scene_analysis(
    project_dir: Path,
    segments_data: list[dict[str, Any]],
    settings: dict[str, Any],
    log_callback: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    base_url = settings.get("lm_url", "http://127.0.0.1:1234/v1")
    model = settings.get("lm_model", "local-model")
    ttl_seconds = _lm_unload_delay_seconds(settings)
    ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else None

    style_anchor = settings.get("style_anchor", "")
    character_description = settings.get("character_description", "")
    niche = settings.get("niche", "documentary")

    t_segments = [pipeline.TranscriptSegment(s["start"], s["end"], s["text"]) for s in segments_data]

    _ensure_lm_context(settings, log)
    log("Analyzing scenes via LM Studio...")
    scenes = pipeline.call_lm_studio_for_segments(
        base_url, model, t_segments, ttl_seconds=ttl,
        style_anchor=style_anchor,
        character_description=character_description,
        niche=niche,
    )

    _maybe_unload_lm(settings, "scene analysis finished", log)

    result = [{"index": s.index, "start": s.start, "end": s.end, "title": s.title,
               "voice_text": s.voice_text, "visual_direction": s.visual_direction,
               "screen_text": s.screen_text, "music_note": s.music_note,
               "retention_note": s.retention_note} for s in scenes]
    return result


def run_shot_generation(
    project_dir: Path,
    scenes_data: list[dict[str, Any]],
    settings: dict[str, Any],
    log_callback: Callable[[str], None] | None = None,
    on_scene_shots: Callable[[list[Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    pipeline_scenes = [pipeline.Scene(**s) for s in scenes_data]
    image_pref = settings.get("image_model_preference", "Auto")
    base_url = settings.get("lm_url", "http://127.0.0.1:1234/v1")
    model = settings.get("lm_model", "local-model")
    ttl_seconds = _lm_unload_delay_seconds(settings)
    ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else None

    style_anchor = settings.get("style_anchor", "")
    character_description = settings.get("character_description", "")
    niche = settings.get("niche", "documentary")

    segments_path = project_dir / "01_transcript_segments.json"
    if segments_path.exists():
        seg_data = json.loads(segments_path.read_text("utf-8"))
        pipeline_segments = [pipeline.TranscriptSegment(s["start"], s["end"], s["text"]) for s in seg_data]
    else:
        pipeline_segments = []

    _ensure_lm_context(settings, log)
    log("Generating shot list via LM Studio...")
    shots = pipeline.call_lm_studio_for_shots(
        base_url, model, pipeline_scenes, image_pref,
        segments=pipeline_segments, ttl_seconds=ttl,
        on_scene_shots=on_scene_shots,
        style_anchor=style_anchor,
        character_description=character_description,
        niche=niche,
    )

    _maybe_unload_lm(settings, "shot list generation finished", log)

    log("Building asset board...")
    cards = pipeline.build_asset_board(project_dir, shots=shots)

    shots_out = [{"index": s.index, "scene_index": s.scene_index,
                  "start": s.start, "end": s.end, "visual_type": s.visual_type,
                  "description": s.description, "asset_query": s.asset_query,
                  "image_model": s.image_model, "video_model": s.video_model,
                  "image_prompt": s.image_prompt, "video_prompt": s.video_prompt,
                  "screen_text": s.screen_text, "motion": s.motion,
                  "priority": s.priority, "fallback": s.fallback} for s in shots]

    return shots_out, _to_cards(cards)



