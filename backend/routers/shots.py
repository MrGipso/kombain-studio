from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from core.config import load_settings
from core.project import open_project

import content_pipeline_gui as cpg
from backend.services.pipeline_orch import run_shot_generation
from backend.services.sse_manager import SSEManager

router = APIRouter()
sse = SSEManager()


@router.get("/{project_id:str}/shots")
async def get_shots(project_id: str):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    shots_data = project.read_json("shot_list") or []
    return shots_data


@router.post("/{project_id:str}/generate-shots")
async def generate_shots(project_id: str):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    scenes_data = project.read_json("scenes")
    if not scenes_data:
        raise HTTPException(400, "No scenes. Run scene analysis first.")

    settings_dict = {f.name: getattr(project.settings, f.name) for f in project.settings.__dataclass_fields__.values()}
    settings_dict["video_style"] = project.metadata.get("video_style", "knitted")
    settings_dict["style_anchor"] = project.metadata.get("style_anchor", "")
    settings_dict["character_description"] = project.metadata.get("character_description", "")

    async def event_stream():
        sub_id, q = sse.subscribe(project.dir.name)
        loop = asyncio.get_event_loop()

        def log_handler(msg: str):
            loop.call_soon_threadsafe(sse.emit, project.dir.name, "log", {"message": msg})

        total_scenes = len(scenes_data) or 1

        def run_sync():
            try:
                accumulated_shots = []
                scenes_done = {"n": 0}
                def _on_scene_shots(raw_shots):
                    accumulated_shots.extend(raw_shots)
                    project.write_json("shot_list", [s.__dict__ for s in accumulated_shots])
                    scenes_done["n"] += 1
                    loop.call_soon_threadsafe(
                        sse.emit, project.dir.name, "scene_shots",
                        {"shots": [s.__dict__ if hasattr(s, '__dict__') else s for s in raw_shots]},
                    )
                    loop.call_soon_threadsafe(
                        sse.emit, project.dir.name, "progress",
                        {"step": "shots", "progress": min(1.0, scenes_done["n"] / total_scenes)},
                    )
                shots, cards = run_shot_generation(
                    project.dir, scenes_data, settings_dict, log_handler,
                    on_scene_shots=_on_scene_shots,
                )
                project.write_json("shot_list", shots)
                cpg.write_asset_board(project.dir, [cpg.AssetCard(**c) for c in cards])
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "done", {"shots": shots, "cards": cards})
            except Exception as exc:
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "error", {"message": str(exc)})

        loop.run_in_executor(None, run_sync)

        async for msg in sse.generator(sub_id):
            yield msg

    return StreamingResponse(event_stream(), media_type="text/event-stream")
