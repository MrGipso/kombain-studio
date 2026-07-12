from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import load_settings
from core.project import (
    Project,
    ProjectExists,
    create_project,
    is_project_dir,
    list_projects,
    open_project,
    safe_project_path,
)
from core.style_presets import DEFAULT_VIDEO_STYLE, list_video_styles

from backend.models.schemas import ProjectInfo
from backend.services import jobs, runlog

router = APIRouter()


class RunLogRecord(BaseModel):
    v: int = 1
    ts: str
    job: str
    msg: str
    run_id: str = ""
    job_id: str = ""
    event: str = "log"
    level: str = "info"
    fields: dict[str, Any] = Field(default_factory=dict)


class ProjectRunLogs(BaseModel):
    items: list[RunLogRecord] = Field(default_factory=list)


@router.post("/{project_id:str}/cancel")
async def cancel_job(project_id: str):
    """Request cancellation of the project's running long job (scene analysis,
    shot generation, transcription, export). Cooperative: the job stops at the
    next step boundary."""
    try:
        path = safe_project_path(project_id, _settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    if not is_project_dir(path):
        raise HTTPException(404, "Project not found")
    name = jobs.request_cancel(path.name)
    return {"ok": True, "cancelled": name or None}


@router.get("/{project_id:str}/jobs")
async def get_project_jobs(project_id: str, limit: int = 25):
    """Durable job status used to reconnect the UI after navigation/reload."""
    try:
        path = safe_project_path(project_id, _settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    if not is_project_dir(path):
        raise HTTPException(404, "Project not found")
    return {
        "running": jobs.running_job(path.name) or None,
        "items": jobs.job_history(path.name, limit=limit),
    }


@router.get("/{project_id:str}/run-logs", response_model=ProjectRunLogs)
async def get_project_run_logs(
    project_id: str,
    limit: int = Query(default=100, ge=1, le=runlog.MAX_READ_RECORDS),
):
    """Return recent structured diagnostics for this project only."""
    try:
        path = safe_project_path(project_id, _settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    if not is_project_dir(path):
        raise HTTPException(404, "Project not found")
    return ProjectRunLogs(items=runlog.recent_records(path, limit=limit))


def _settings() -> Any:
    return load_settings()


@router.get("")
async def get_projects():
    s = _settings()
    output_base = Path(s.output_base or "projects")
    projects = list_projects(output_base)
    return [
        ProjectInfo(
            name=p.name,
            dir=str(p.dir),
            preset=p.preset,
            language=p.metadata.get("language", "ru"),
        )
        for p in projects
    ]


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    preset: str = "universal"
    language: str = Field(default="ru", min_length=2, max_length=16)


@router.post("")
async def create_project_ep(req: CreateProjectRequest):
    s = _settings()
    output_base = Path(s.output_base or "projects")
    if not (req.name or "").strip():
        raise HTTPException(400, "Название проекта не может быть пустым")
    try:
        project = create_project(
            name=req.name,
            output_base=output_base,
            settings=s,
            preset=Path(req.preset or "universal").name,
        )
    except ProjectExists as exc:
        raise HTTPException(409, str(exc))
    project.settings.whisper_language = req.language
    project.save_meta()
    return ProjectInfo(
        name=project.name,
        dir=str(project.dir),
        preset=project.preset,
        language=req.language,
    )


@router.get("/styles")
async def get_video_styles():
    return list_video_styles()


@router.get("/{project_id:str}")
async def get_project(project_id: str):
    try:
        path = safe_project_path(project_id, _settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    if not path.exists():
        raise HTTPException(404, "Project not found")
    project = open_project(path, _settings())
    if not project:
        raise HTTPException(404, "Project not found")

    cards_data = project.read_json("asset_board") or []
    scenes_data = project.read_json("scenes") or []
    shots_data = project.read_json("shot_list") or []
    segments_data = project.read_json("transcript_segments") or []
    transcript_text = ""
    transcript_path = project.file_path("transcript")
    if transcript_path and transcript_path.exists():
        transcript_text = transcript_path.read_text("utf-8")

    return {
        "project": ProjectInfo(
            name=project.name,
            dir=str(project.dir),
            preset=project.preset,
            language=project.metadata.get("language", "ru"),
        ),
        "scenes": scenes_data,
        "shots": shots_data,
        "asset_board": cards_data,
        "segments": segments_data,
        "transcript": transcript_text,
        "video_style": project.metadata.get("video_style", DEFAULT_VIDEO_STYLE),
        "style_anchor": project.metadata.get("style_anchor", ""),
        "character_description": project.metadata.get("character_description", ""),
        "seg_min_seconds": project.metadata.get("seg_min_seconds", 20),
        "seg_max_seconds": project.metadata.get("seg_max_seconds", 90),
        "seg_sensitivity": project.metadata.get("seg_sensitivity", 0.6),
        "seg_target_scenes": project.metadata.get("seg_target_scenes", 0),
        "shot_mode": project.metadata.get("shot_mode", "stock" if project.metadata.get("stock_mode") else "ai"),
        "route_priority": project.metadata.get("route_priority", "balanced"),
        "stock_mode": project.metadata.get("stock_mode", False),
        "stock_cut_seconds": project.metadata.get("stock_cut_seconds", 5),
    }


class UpdateProjectSettingsRequest(BaseModel):
    # All optional: this is a partial update, so a page can save just the fields
    # it owns without clobbering settings owned by another page.
    video_style: str | None = None
    style_anchor: str | None = None
    character_description: str | None = None
    seg_min_seconds: int | None = Field(default=None, ge=1, le=600)
    seg_max_seconds: int | None = Field(default=None, ge=1, le=1800)
    seg_sensitivity: float | None = Field(default=None, ge=0.0, le=1.0)
    seg_target_scenes: int | None = Field(default=None, ge=0, le=1000)
    shot_mode: Literal["ai", "stock", "hybrid"] | None = None
    route_priority: Literal["economy", "balanced", "rich"] | None = None
    stock_mode: bool | None = None
    stock_cut_seconds: int | None = Field(default=None, ge=1, le=60)


@router.put("/{project_id:str}/settings")
async def update_project_settings(project_id: str, req: UpdateProjectSettingsRequest):
    try:
        path = safe_project_path(project_id, _settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    try:
        with jobs.project_mutation(path.name, "Настройки проекта"):
            # Re-open only after acquiring the project lock.  Otherwise a
            # background stage can start after the initial read and our save
            # would overwrite metadata/manifest changes it just produced.
            project = open_project(path, _settings())
            if not project:
                raise HTTPException(404, "Project not found")
            updates = req.model_dump(exclude_none=True)
            next_min = int(updates.get("seg_min_seconds", project.metadata.get("seg_min_seconds", 20)))
            next_max = int(updates.get("seg_max_seconds", project.metadata.get("seg_max_seconds", 90)))
            if next_min > next_max:
                raise HTTPException(422, "seg_min_seconds must be less than or equal to seg_max_seconds")
            changed = {
                key for key, value in updates.items()
                if project.metadata.get(key) != value
            }
            for key, value in updates.items():
                project.metadata[key] = value
            project.save_meta()
            scene_inputs = {
                "video_style", "style_anchor", "character_description",
                "seg_min_seconds", "seg_max_seconds", "seg_sensitivity", "seg_target_scenes",
            }
            shot_inputs = {"shot_mode", "route_priority", "stock_mode", "stock_cut_seconds"}
            if changed & scene_inputs:
                project.invalidate_downstream("transcript", reason="project scene settings changed")
            elif changed & shot_inputs:
                project.invalidate_downstream("scenes", reason="project shot settings changed")
            m = project.metadata
            return {
                "video_style": m.get("video_style", DEFAULT_VIDEO_STYLE),
                "style_anchor": m.get("style_anchor", ""),
                "character_description": m.get("character_description", ""),
                "seg_min_seconds": m.get("seg_min_seconds", 20),
                "seg_max_seconds": m.get("seg_max_seconds", 90),
                "seg_sensitivity": m.get("seg_sensitivity", 0.6),
                "seg_target_scenes": m.get("seg_target_scenes", 0),
                "shot_mode": m.get("shot_mode", "ai"),
                "route_priority": m.get("route_priority", "balanced"),
                "stock_mode": m.get("stock_mode", False),
                "stock_cut_seconds": m.get("stock_cut_seconds", 5),
            }
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))


@router.get("/{project_id:str}/state")
async def get_project_state(project_id: str):
    try:
        path = safe_project_path(project_id, _settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    if not path.exists():
        raise HTTPException(404, "Project not found")
    project = open_project(path, _settings())
    if not project:
        raise HTTPException(404, "Project not found")
    stage_states = project.stage_states()
    completed = lambda stage: stage_states[stage]["status"] == "completed"
    # Keep the historical boolean keys for current clients while exposing the
    # richer versioned states to new UI components.
    steps = {
        "transcript": completed("transcript"),
        "transcript_segments": completed("transcript"),
        "scenes": completed("scenes"),
        "shot_list": completed("shots"),
        "asset_board": completed("assets"),
        "validation_report": completed("validation"),
        "export": completed("export"),
    }
    return {
        "project_dir": str(path),
        "schema_version": project.schema_version,
        "steps": steps,
        "stage_states": stage_states,
    }
