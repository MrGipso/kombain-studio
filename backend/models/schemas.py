from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectMeta(BaseModel):
    name: str
    preset: str = "universal"
    language: str = "ru"
    dir: str = ""


class ProjectInfo(BaseModel):
    name: str
    dir: str
    preset: str = "universal"
    language: str = "ru"


class ProjectSettingsUpdate(BaseModel):
    video_style: str = "knitted"
    style_anchor: str = ""
    character_description: str = ""


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
    sfx_query: str = ""
    sfx_accent_query: str = ""
    reference_query: str = ""
    reference_source: str = "wikipedia"
    segment_ids: list[int] = Field(default_factory=list)
    visual_concept_id: str = ""
    purpose: str = ""
    continuity_mode: str = "independent"
    continuity_group_id: str = ""
    location_id: str = ""
    subject_ids: list[str] = Field(default_factory=list)
    state_before: str = ""
    action: str = ""
    state_after: str = ""
    shot_size: str = ""
    camera_angle: str = ""
    screen_direction: str = ""
    reference_frame_id: str = ""
    transition_in: str = ""
    transition_out: str = ""
    change_from_previous: str = ""


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
    text_overlay_enabled: bool = False
    text_overlay_text: str = ""
    text_overlay_position: str = "bottom"
    text_overlay_style: str = "subtitle"
    text_overlay_x: float = 50.0
    text_overlay_y: float = 86.0
    text_overlay_enter: str = "fade"
    text_overlay_exit: str = "fade"
    source_url: str = ""
    source_license: str = ""
    source_author: str = ""
    stock_score: float = 0.0
    sfx_query: str = ""
    sfx_output: str = ""
    sfx_accent_query: str = ""
    sfx_accent_output: str = ""
    priority: str = "medium"
    qa_score: float = -1.0
    ltx_draft: bool = False
    # Reference-insert route (Wikipedia / arXiv): the LLM names an entity, the
    # backend resolves it to a real page, screenshots it and slides it in over a
    # blurred backdrop. reference_query/source come from the planner; the rest are
    # filled in at generation time.
    reference_query: str = ""
    reference_source: str = "wikipedia"
    reference_url: str = ""
    reference_output: str = ""
    reference_credit: str = ""
    plan_fingerprint: str = ""
    purpose: str = ""
    continuity_mode: str = "independent"
    continuity_group_id: str = ""
    location_id: str = ""
    subject_ids: list[str] = Field(default_factory=list)
    state_before: str = ""
    action: str = ""
    state_after: str = ""
    shot_size: str = ""
    camera_angle: str = ""
    screen_direction: str = ""
    reference_frame_id: str = ""
    transition_in: str = ""
    transition_out: str = ""
    change_from_previous: str = ""
    card_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class AssetVariant(BaseModel):
    """One immutable media snapshot plus the inputs that produced it."""

    id: str
    created_at: str
    card_id: str = ""
    card_index: int
    shot_index: int
    source: str
    mode: str
    output_field: Literal[
        "image_output",
        "video_output",
        "reference_output",
        "remotion_output",
        "assigned_media",
    ]
    file_path: str
    file_hash: str
    file_size: int
    media_type: str
    parent_id: str | None = None
    prompt: str = ""
    query: str = ""
    model: str = ""
    settings_hash: str = ""
    model_hash: str = ""
    workflow: str = ""
    workflow_hash: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    selected: bool = False


class AssetVariantSelections(BaseModel):
    image_output: str | None = None
    video_output: str | None = None
    reference_output: str | None = None
    remotion_output: str | None = None
    assigned_media: str | None = None


class AssetVariantList(BaseModel):
    card_id: str = ""
    card_index: int
    shot_index: int
    selections: AssetVariantSelections
    variants: list[AssetVariant] = Field(default_factory=list)


class AssetVariantSelectResult(BaseModel):
    ok: bool = True
    card: AssetCard
    variant: AssetVariant
    selections: AssetVariantSelections


class Settings(BaseModel):
    lm_url: str = "http://127.0.0.1:1234/v1"
    lm_model: str = "local-model"
    lm_embed_model: str = "text-embedding-nomic-embed-text-v1.5"
    lm_context: str = "32768"
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_watchdog_enabled: bool = False
    comfy_install_dir: str = ""
    comfy_watchdog_listen_address: str = "127.0.0.1"
    comfy_watchdog_sage_attention: bool = True
    comfy_watchdog_poll_seconds: float = 5.0
    comfy_watchdog_failure_threshold: int = 3
    comfy_watchdog_startup_timeout_seconds: int = 180
    comfy_watchdog_max_restarts: int = 3
    comfy_watchdog_restart_window_seconds: int = 1800
    comfy_krea_workflow: str = ""
    comfy_ideogram_workflow: str = ""
    comfy_ltx_workflow: str = ""
    image_model_preference: str = "Auto"
    whisper_cli: str = ""
    whisper_model: str = ""
    ffmpeg: str = ""
    whisper_language: str = "auto"
    omnivoice_cli: str = "omnivoice-infer"
    omnivoice_model: str = "k2-fsa/OmniVoice"
    output_base: str = ""
    lm_auto_unload: bool = True
    lm_unload_delay: str = "0"
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    lm_vision_model: str = ""
    stock_score_threshold: float = 0.6
    stock_floor: float = 0.4
    stock_retry_queries: int = 1
    stock_max_seconds: int = 0
    stock_candidates: int = 20
    stock_uniquify: bool = True
    stock_mirror: bool = False
    reference_enable: bool = True
    reference_density: str = "normal"


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


ExportMode = Literal["draft", "master"]


class ExportGateReason(BaseModel):
    """One stable, machine-readable reason why a Master export is blocked."""

    code: str
    message: str
    card_index: int | None = None
    shot_index: int | None = None
    item: str = ""
    details: dict[str, Any] | None = None


class ExportGateSummary(BaseModel):
    blockers: int = 0
    validation_errors: int = 0
    cards_total: int = 0
    cards_required: int = 0
    cards_ready: int = 0
    cards_excluded: int = 0


class ExportGateReport(BaseModel):
    mode: ExportMode
    allowed: bool
    summary: ExportGateSummary
    reasons: list[ExportGateReason] = Field(default_factory=list)


class MasterManifestInfo(BaseModel):
    path: str
    sha256: str
    size: int
    manifest_id: str
    created_at: str
    output_sha256: str


class QueueStatus(BaseModel):
    running: bool
    queued: int
    completed: int
    failed: int
    items: list[dict[str, Any]] = Field(default_factory=list)


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
    items: list[TimelineMediaItem] = Field(default_factory=list)


class SSELog(BaseModel):
    event: str = "log"
    data: dict[str, Any]
