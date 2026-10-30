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

from backend.services.pipeline_orch import run_shot_generation, shot_stage_input_hash
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
                        stage_input_hash = shot_stage_input_hash(
                            scenes_data,
                            settings_dict,
                        )
                        partial_path = current_project.dir / "04_shot_list.partial.json"
                        checkpoint_path = (
                            current_project.dir / ".shot_generation_checkpoint.json"
                        )
                        resume_shots: list[dict[str, Any]] = []
                        try:
                            checkpoint = current_project.read_json(
                                ".shot_generation_checkpoint.json"
                            )
                            partial = current_project.read_json(
                                "04_shot_list.partial.json"
                            )
                            if (
                                isinstance(checkpoint, dict)
                                and checkpoint.get("input_hash") == stage_input_hash
                                and isinstance(partial, list)
                            ):
                                resume_shots = [
                                    item for item in partial if isinstance(item, dict)
                                ]
                        except (OSError, ValueError, TypeError):
                            resume_shots = []

                        resumed_scene_ids: list[int] = []
                        for scene in scenes_data:
                            scene_index = int(scene.get("index", 0) or 0)
                            if any(
                                int(item.get("scene_index", 0) or 0) == scene_index
                                for item in resume_shots
                            ):
                                resumed_scene_ids.append(scene_index)
                            else:
                                break
                        resume_set = set(resumed_scene_ids)
                        resume_shots = [
                            item
                            for item in resume_shots
                            if int(item.get("scene_index", 0) or 0) in resume_set
                        ]
                        accumulated_shots = list(resume_shots)
                        scenes_done = {"n": len(resumed_scene_ids)}
                        job_log = jobs.checking_logger(
                            runlog.tee(
                                current_project.dir,
                                "Генерация шот-листа",
                                log_handler,
                            ),
                            cancel_event,
                        )
                        if resume_shots:
                            job_log(
                                f"Возобновление с контрольной точки: "
                                f"готово сцен {len(resumed_scene_ids)} из "
                                f"{total_scenes}, кадров {len(resume_shots)}."
                            )
                            loop.call_soon_threadsafe(
                                sse.emit_to,
                                sub_id,
                                "scene_shots",
                                {"shots": resume_shots, "resumed": True},
                            )

                        def _on_progress(step: str, progress: float) -> None:
                            labels = {
                                "global_plan": "Глобальный план фильма",
                                "scenes": (
                                    f"Сцена {scenes_done['n']} из {total_scenes}"
                                ),
                                "finalize": "Финальная сборка шот-листа",
                            }
                            jobs.update_progress(
                                cancel_event,
                                max(0.0, min(1.0, float(progress))),
                                labels.get(step, step),
                            )
                            loop.call_soon_threadsafe(
                                sse.emit_to,
                                sub_id,
                                "progress",
                                {
                                    "step": step,
                                    "progress": max(0.0, min(1.0, float(progress))),
                                },
                            )

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
                            current_project.write_json(
                                ".shot_generation_checkpoint.json",
                                {
                                    "schema_version": 1,
                                    "input_hash": stage_input_hash,
                                    "scenes_completed": scenes_done["n"],
                                    "shots_completed": len(accumulated_shots),
                                },
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

                        shots, cards = run_shot_generation(
                            current_project.dir,
                            scenes_data,
                            settings_dict,
                            job_log,
                            on_scene_shots=_on_scene_shots,
                            resume_shots=resume_shots,
                            on_progress=_on_progress,
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
                            input_hash=stage_input_hash,
                        )
                        current_project.mark_stage_completed(
                            "assets",
                            cards,
                            input_hash=current_project.content_hash(shots),
                        )
                        partial_path.unlink(missing_ok=True)
                        checkpoint_path.unlink(missing_ok=True)
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
