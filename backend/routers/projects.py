from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.config import load_settings
from core.project import Project, create_project, open_project, list_projects
from core.style_presets import DEFAULT_VIDEO_STYLE, list_video_styles

from backend.models.schemas import ProjectInfo, ProjectSettingsUpdate, Settings

router = APIRouter()


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
    name: str
    preset: str = "universal"
    language: str = "ru"


@router.post("")
async def create_project_ep(req: CreateProjectRequest):
    s = _settings()
    output_base = Path(s.output_base or "projects")
    project = create_project(
        name=req.name,
        output_base=output_base,
        settings=s,
        preset="universal",
    )
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
    path = Path(project_id)
    if not path.is_absolute():
        s = _settings()
        output_base = Path(s.output_base or "projects")
        path = output_base / project_id
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
    }


class UpdateProjectSettingsRequest(BaseModel):
    video_style: str = DEFAULT_VIDEO_STYLE
    style_anchor: str = ""
    character_description: str = ""
    seg_min_seconds: int = 20
    seg_max_seconds: int = 90
    seg_sensitivity: float = 0.6
    seg_target_scenes: int = 0


@router.put("/{project_id:str}/settings")
async def update_project_settings(project_id: str, req: UpdateProjectSettingsRequest):
    path = Path(project_id)
    if not path.is_absolute():
        s = _settings()
        output_base = Path(s.output_base or "projects")
        path = output_base / project_id
    if not path.exists():
        raise HTTPException(404, "Project not found")
    project = open_project(path, _settings())
    if not project:
        raise HTTPException(404, "Project not found")
    project.metadata["video_style"] = req.video_style or DEFAULT_VIDEO_STYLE
    project.metadata["style_anchor"] = req.style_anchor
    project.metadata["character_description"] = req.character_description
    project.metadata["seg_min_seconds"] = req.seg_min_seconds
    project.metadata["seg_max_seconds"] = req.seg_max_seconds
    project.metadata["seg_sensitivity"] = req.seg_sensitivity
    project.metadata["seg_target_scenes"] = req.seg_target_scenes
    project.save_meta()
    return {
        "video_style": project.metadata["video_style"],
        "style_anchor": req.style_anchor,
        "character_description": req.character_description,
        "seg_min_seconds": req.seg_min_seconds,
        "seg_max_seconds": req.seg_max_seconds,
        "seg_sensitivity": req.seg_sensitivity,
        "seg_target_scenes": req.seg_target_scenes,
    }


@router.get("/{project_id:str}/state")
async def get_project_state(project_id: str):
    path = Path(project_id)
    if not path.is_absolute():
        s = _settings()
        output_base = Path(s.output_base or "projects")
        path = output_base / project_id
    if not path.exists():
        raise HTTPException(404, "Project not found")

    steps = {}
    for key, filename in [
        ("transcript", "01_transcript.txt"),
        ("transcript_segments", "01_transcript_segments.json"),
        ("scenes", "02_scenes.json"),
        ("shot_list", "04_shot_list.json"),
        ("asset_board", "06_asset_board.json"),
        ("validation_report", "07_validation_report.json"),
    ]:
        file_path = path / filename
        steps[key] = file_path.exists()

    return {"project_dir": str(path), "steps": steps}
