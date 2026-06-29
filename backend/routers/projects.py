from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.config import load_settings, list_presets
from core.project import Project, create_project, open_project, list_projects

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
    preset: str = "documentary"
    language: str = "ru"


@router.post("")
async def create_project_ep(req: CreateProjectRequest):
    s = _settings()
    output_base = Path(s.output_base or "projects")
    project = create_project(
        name=req.name,
        output_base=output_base,
        settings=s,
        preset=req.preset,
    )
    project.settings.whisper_language = req.language
    project.save_meta()
    return ProjectInfo(
        name=project.name,
        dir=str(project.dir),
        preset=project.preset,
        language=req.language,
    )


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
        "style_anchor": project.metadata.get("style_anchor", ""),
        "character_description": project.metadata.get("character_description", ""),
        "niche": project.metadata.get("niche", "documentary"),
    }


class UpdateProjectSettingsRequest(BaseModel):
    style_anchor: str = ""
    character_description: str = ""
    niche: str = "documentary"


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
    project.metadata["style_anchor"] = req.style_anchor
    project.metadata["character_description"] = req.character_description
    project.metadata["niche"] = req.niche
    project.save_meta()
    return {
        "style_anchor": req.style_anchor,
        "character_description": req.character_description,
        "niche": req.niche,
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
