from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from core.config import load_settings
from core import continuity
from core.board import write_asset_board
from core.models import AssetCard
from core.project import open_project, safe_project_path

from backend.services.pipeline_orch import run_shot_generation
from backend.services.sse_manager import SSEManager
from backend.services import jobs, runlog

router = APIRouter()
sse = SSEManager()


@router.get("/{project_id:str}/shots")
async def get_shots(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    shots_data = project.read_json("shot_list") or []
    return shots_data


@router.get("/{project_id:str}/context-bible")
async def get_context_bible(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")
    bible = continuity.load_context_bible(project.dir)
    if bible is None:
        raise HTTPException(
            404,
            "Context Bible ещё не создана; заново выполните анализ кадров.",
        )
    return bible


@router.post("/{project_id:str}/generate-shots")
async def generate_shots(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    if not project.read_json("scenes"):
        raise HTTPException(400, "No scenes. Run scene analysis first.")

    async def event_stream():
        sub_id, q = sse.subscribe(project.dir.name)
        loop = asyncio.get_event_loop()

        def log_handler(msg: str):
            loop.call_soon_threadsafe(sse.emit_to, sub_id, "log", {"message": msg})

        def run_sync():
            try:
                with jobs.project_job(project.dir.name, "Генерация шот-листа", resource="llm") as cancel_event:
                    current_project = open_project(project.dir, load_settings()) or project
                    scenes_data = current_project.read_json("scenes")
                    if not isinstance(scenes_data, list) or not scenes_data:
                        raise ValueError("No scenes. Run scene analysis first.")
                    settings_dict = current_project.effective_settings()
                    total_scenes = len(scenes_data)
                    try:
                        accumulated_shots = []
                        scenes_done = {"n": 0}

                        def _on_scene_shots(raw_shots):
                            # Per-scene hook doubles as a cancellation checkpoint:
                            # the LLM loop stops after the current scene.
                            jobs.check(cancel_event)
                            accumulated_shots.extend(raw_shots)
                            current_project.write_json(
                                "04_shot_list.partial.json",
                                [s.__dict__ for s in accumulated_shots],
                            )
                            scenes_done["n"] += 1
                            progress = min(1.0, scenes_done["n"] / total_scenes)
                            jobs.update_progress(
                                cancel_event,
                                progress,
                                f"Сцена {scenes_done['n']} из {total_scenes}",
                            )
                            loop.call_soon_threadsafe(
                                sse.emit_to,
                                sub_id,
                                "scene_shots",
                                {"shots": [
                                    shot.__dict__ if hasattr(shot, "__dict__") else shot
                                    for shot in raw_shots
                                ]},
                            )
                            loop.call_soon_threadsafe(
                                sse.emit_to,
                                sub_id,
                                "progress",
                                {"step": "shots", "progress": progress},
                            )

                        shots, cards = run_shot_generation(
                            current_project.dir,
                            scenes_data,
                            settings_dict,
                            jobs.checking_logger(
                                runlog.tee(
                                    current_project.dir,
                                    "Генерация шот-листа",
                                    log_handler,
                                ),
                                cancel_event,
                            ),
                            on_scene_shots=_on_scene_shots,
                        )
                        jobs.check(cancel_event)
                        current_project.write_json("shot_list", shots)
                        write_asset_board(
                            current_project.dir,
                            [AssetCard(**card) for card in cards],
                        )
                        current_project.mark_stage_completed(
                            "shots",
                            shots,
                            input_hash=current_project.content_hash({
                                "scenes": scenes_data,
                                "settings": {
                                    key: settings_dict.get(key)
                                    for key in (
                                        "lm_model", "lm_context", "video_style", "style_anchor",
                                        "character_description", "shot_mode", "route_priority",
                                        "stock_mode", "stock_cut_seconds", "image_model_preference",
                                    )
                                },
                            }),
                        )
                        current_project.mark_stage_completed(
                            "assets",
                            cards,
                            input_hash=current_project.content_hash(shots),
                        )
                        (current_project.dir / "04_shot_list.partial.json").unlink(
                            missing_ok=True
                        )
                    except jobs.JobCancelled:
                        raise
                    except Exception as exc:
                        current_project.mark_stage_failed(
                            "shots",
                            str(exc),
                            input_hash=current_project.content_hash(scenes_data),
                        )
                        raise
                loop.call_soon_threadsafe(
                    sse.emit_to,
                    sub_id,
                    "done",
                    {"shots": shots, "cards": cards},
                )
            except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "error", {"message": str(exc)})
            except Exception as exc:
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "error", {"message": str(exc)})

        loop.run_in_executor(None, run_sync)

        async for msg in sse.generator(sub_id):
            yield msg

    return StreamingResponse(event_stream(), media_type="text/event-stream")
