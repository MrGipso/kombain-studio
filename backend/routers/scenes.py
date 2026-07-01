from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from core.config import load_settings
from core.project import open_project

from backend.services.pipeline_orch import run_scene_analysis
from backend.services.sse_manager import SSEManager

router = APIRouter()
sse = SSEManager()


@router.get("/{project_id:str}/scenes")
async def get_scenes(project_id: str):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    scenes_data = project.read_json("scenes") or []
    return scenes_data


@router.post("/{project_id:str}/analyze-scenes")
async def analyze_scenes(project_id: str):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    segments_data = project.read_json("transcript_segments")
    if not segments_data:
        raise HTTPException(400, "No transcript segments. Run transcription first.")

    settings_dict = {f.name: getattr(project.settings, f.name) for f in project.settings.__dataclass_fields__.values()}
    settings_dict["video_style"] = project.metadata.get("video_style", "knitted")
    settings_dict["style_anchor"] = project.metadata.get("style_anchor", "")
    settings_dict["character_description"] = project.metadata.get("character_description", "")
    settings_dict["seg_min_seconds"] = project.metadata.get("seg_min_seconds", 20)
    settings_dict["seg_max_seconds"] = project.metadata.get("seg_max_seconds", 90)
    settings_dict["seg_sensitivity"] = project.metadata.get("seg_sensitivity", 0.6)
    settings_dict["seg_target_scenes"] = project.metadata.get("seg_target_scenes", 0)

    async def event_stream():
        sub_id, q = sse.subscribe(project.dir.name)
        loop = asyncio.get_event_loop()

        def log_handler(msg: str):
            loop.call_soon_threadsafe(sse.emit, project.dir.name, "log", {"message": msg})

        def run_sync():
            try:
                scenes = run_scene_analysis(
                    project.dir, segments_data, settings_dict, log_handler,
                )
                project.write_json("scenes", scenes)
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "done", {"scenes": scenes})
            except Exception as exc:
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "error", {"message": str(exc)})

        loop.run_in_executor(None, run_sync)

        async for msg in sse.generator(sub_id):
            yield msg

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/{project_id:str}/export-scenes")
async def export_scenes(project_id: str):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    scenes = project.read_json("scenes") or []
    if not scenes:
        raise HTTPException(400, "No scenes to export")

    # Write edit_plan CSV
    import csv, io
    csv_path = project.dir / "edit_plan.csv"
    with open(str(csv_path), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Scene", "Start", "End", "Title", "Visual Direction", "Screen Text"])
        for s in scenes:
            writer.writerow([s.get("index"), s.get("start"), s.get("end"),
                             s.get("title"), s.get("visual_direction"), s.get("screen_text")])

    # Write visual_plan.md
    md_path = project.dir / "visual_plan.md"
    lines = ["# Visual Plan\n"]
    for s in scenes:
        lines.append(f"## Scene {s.get('index')}: {s.get('title')} ({s.get('start')} - {s.get('end')})")
        lines.append(f"- **Visual:** {s.get('visual_direction', '')}")
        lines.append(f"- **Screen:** {s.get('screen_text', '')}")
        lines.append(f"- **Music:** {s.get('music_note', '')}")
        lines.append(f"- **Retention:** {s.get('retention_note', '')}")
        lines.append("")
    md_path.write_text("\n".join(lines), "utf-8")

    return {"ok": True, "csv": str(csv_path), "md": str(md_path), "count": len(scenes)}


@router.put("/{project_id:str}/scenes/{scene_index}")
async def update_scene(project_id: str, scene_index: int, data: dict[str, Any]):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    scenes = project.read_json("scenes") or []
    for scene in scenes:
        if scene.get("index") == scene_index:
            scene.update(data)
            project.write_json("scenes", scenes)
            return scene
    raise HTTPException(404, "Scene not found")
