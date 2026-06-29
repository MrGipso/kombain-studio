from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ProjectMeta(BaseModel):
    name: str
    preset: str = "documentary"
    language: str = "ru"
    dir: str = ""


class ProjectInfo(BaseModel):
    name: str
    dir: str
    preset: str = "documentary"
    language: str = "ru"


class ProjectSettingsUpdate(BaseModel):
    style_anchor: str = ""
    character_description: str = ""
    niche: str = "documentary"


class TranscriptSegment(BaseModel):
    start: str
    end: str
    text: str


class Scene(BaseModel):
    index: int
    start: str
    end: str
    title: str
    voice_text: str = ""
    visual_direction: str = ""
    screen_text: str = ""
    music_note: str = ""
    retention_note: str = ""


class Shot(BaseModel):
    index: int
    scene_index: int
    start: str
    end: str
    visual_type: str = ""
    description: str = ""
    asset_query: str = ""
    image_model: str = ""
    video_model: str = ""
    image_prompt: str = ""
    video_prompt: str = ""
    screen_text: str = ""
    motion: str = ""
    priority: str = ""
    fallback: str = ""


class AssetCard(BaseModel):
    shot_index: int
    scene_index: int
    scene_title: str = ""
    start: str = ""
    end: str = ""
    visual_type: str = ""
    route: str = ""
    description: str = ""
    asset_query: str = ""
    image_model: str = ""
    video_model: str = ""
    remotion_template: str = ""
    image_prompt: str = ""
    video_prompt: str = ""
    screen_text: str = ""
    motion: str = ""
    fallback: str = ""
    image_output: str = ""
    video_output: str = ""
    remotion_output: str = ""
    status: str = "planned"
    notes: str = ""
    approved: bool = False
    timeline_start: str = ""
    timeline_end: str = ""
    assigned_media: str = ""
    track: str = "video_1"


class Settings(BaseModel):
    lm_url: str = "http://127.0.0.1:1234/v1"
    lm_model: str = "local-model"
    lm_context: str = "32768"
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_krea_workflow: str = ""
    comfy_ideogram_workflow: str = ""
    comfy_ltx_workflow: str = ""
    image_model_preference: str = "Auto"
    whisper_cli: str = ""
    whisper_model: str = ""
    ffmpeg: str = ""
    whisper_language: str = "ru"
    output_base: str = ""
    lm_auto_unload: bool = True
    lm_unload_delay: str = "0"


class ValidationIssue(BaseModel):
    level: str
    stage: str
    message: str
    item: str = ""
    details: dict[str, Any] | None = None


class ValidationReport(BaseModel):
    project: str
    summary: dict[str, Any]
    issues: list[ValidationIssue]


class ExportResult(BaseModel):
    success: bool
    path: str | None = None
    error: str | None = None


class QueueStatus(BaseModel):
    running: bool
    queued: int
    completed: int
    failed: int
    items: list[dict[str, Any]] = []


class PipelineStepStatus(BaseModel):
    name: str
    status: str = "pending"
    progress: float = 0.0
    error: str | None = None


class TimelineUpdate(BaseModel):
    cards: list[AssetCard]


class TimelineMediaItem(BaseModel):
    card_index: int
    shot_index: int
    scene_index: int
    start: str = ""
    end: str = ""
    title: str = ""
    status: str = ""
    approved: bool = False
    route: str = ""
    visual_type: str = ""
    media_type: str = ""
    path: str = ""
    url: str = ""
    poster_url: str = ""
    duration: float | None = None


class TimelineMedia(BaseModel):
    project_dir: str
    voice_path: str = ""
    voice_url: str = ""
    export_path: str = ""
    export_url: str = ""
    items: list[TimelineMediaItem] = []


class SSELog(BaseModel):
    event: str = "log"
    data: dict[str, Any]
