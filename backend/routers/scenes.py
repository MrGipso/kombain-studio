from __future__ import annotations

import asyncio
import csv
import io
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from core.config import load_settings
from core import script_align
from core.models import TranscriptSegment
from core.project import atomic_write_text, open_project, safe_project_path

from backend.services.pipeline_orch import run_scene_analysis
from backend.services.sse_manager import SSEManager
from backend.services import jobs, runlog
from backend.models.schemas import Scene as SceneSchema

router = APIRouter()
sse = SSEManager()

SCENE_INPUT_SETTING_KEYS = (
    "lm_url",
    "lm_model",
    "lm_embed_model",
    "lm_context",
    "video_style",
    "style_anchor",
    "character_description",
    "seg_min_seconds",
    "seg_max_seconds",
    "seg_sensitivity",
    "seg_target_scenes",
)


def _scene_input_payload(project, segments_data: list[dict], settings: dict[str, Any]) -> dict[str, Any]:
    """Describe exactly the inputs that can influence scene analysis.

    An authored script is conditional: ``run_scene_analysis`` uses it only when
    it contains enough beats *and* those beats align to this transcript.  The
    same predicate is applied here so an unrelated/unfinished script neither
    blocks an uploaded transcript nor makes its scene result spuriously stale.
    """
    payload: dict[str, Any] = {
        "segments": segments_data,
        "settings": {key: settings.get(key) for key in SCENE_INPUT_SETTING_KEYS},
    }
    beats = script_align.load_script_beats(project.dir)
    if not beats:
        return payload
    try:
        segments = [
            TranscriptSegment(
                start=str(segment.get("start", "")),
                end=str(segment.get("end", "")),
                text=str(segment.get("text", "")),
            )
            for segment in segments_data
        ]
    except (AttributeError, TypeError):
        return payload
    if not script_align.align_beats_to_segments(beats, segments):
        return payload
    script_path = project.dir / "00_script.json"
    try:
        script_hash = project.content_hash(script_path)
    except (FileNotFoundError, OSError):
        # The script is optional and may have been removed by another editor
        # between loading and hashing it. The analyzer will take the same
        # transcript-only fallback in that case.
        return payload
    payload["script"] = {"artifact": script_path.name, "hash": script_hash}
    return payload


@router.get("/{project_id:str}/scenes")
async def get_scenes(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    scenes_data = project.read_json("scenes") or []
    return scenes_data


@router.post("/{project_id:str}/analyze-scenes")
async def analyze_scenes(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    if not project.read_json("transcript_segments"):
        raise HTTPException(400, "No transcript segments. Run transcription first.")

    async def event_stream():
        sub_id, q = sse.subscribe(project.dir.name)
        loop = asyncio.get_event_loop()

        def log_handler(msg: str):
            loop.call_soon_threadsafe(sse.emit, project.dir.name, "log", {"message": msg})

        def run_sync():
            scene_input_hash = ""
            try:
                with jobs.project_job(project.dir.name, "Анализ сцен", resource="llm") as cancel_event:
                    # The preliminary HTTP check only improves the immediate
                    # error response. Inputs used by the worker must be read
                    # again under the project lock so analysis never commits a
                    # result built from a pre-lock transcript/settings snapshot.
                    current_project = open_project(path, load_settings())
                    if not current_project:
                        raise RuntimeError("Project not found")
                    segments_data = current_project.read_json("transcript_segments")
                    if not segments_data:
                        raise ValueError("No transcript segments. Run transcription first.")
                    settings_dict = current_project.effective_settings()
                    scene_inputs = _scene_input_payload(current_project, segments_data, settings_dict)
                    scene_input_hash = current_project.content_hash(scene_inputs)
                    try:
                        scenes = run_scene_analysis(
                            current_project.dir, segments_data, settings_dict,
                            jobs.checking_logger(
                                runlog.tee(current_project.dir, "Анализ сцен", log_handler),
                                cancel_event,
                            ),
                        )
                        jobs.check(cancel_event)
                        current_project.write_json("scenes", scenes)
                        current_project.mark_stage_completed(
                            "scenes",
                            scenes,
                            input_hash=scene_input_hash,
                            metadata={"script_input": "script" in scene_inputs},
                        )
                    except jobs.JobCancelled:
                        raise
                    except Exception as exc:
                        # Failure state is itself a manifest mutation and must
                        # be recorded before releasing the same project lock.
                        current_project.mark_stage_failed(
                            "scenes", str(exc), input_hash=scene_input_hash or None,
                        )
                        raise
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "done", {"scenes": scenes})
            except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "error", {"message": str(exc)})
            except Exception as exc:
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "error", {"message": str(exc)})

        loop.run_in_executor(None, run_sync)

        async for msg in sse.generator(sub_id):
            yield msg

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/{project_id:str}/export-scenes")
async def export_scenes(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")

    try:
        with jobs.project_mutation(path.name, "Экспорт плана сцен"):
            project = open_project(path, load_settings())
            if not project:
                raise HTTPException(404, "Project not found")
            scenes = project.read_json("scenes") or []
            if not scenes:
                raise HTTPException(400, "No scenes to export")

            csv_path = project.dir / "edit_plan.csv"
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            writer.writerow(["Scene", "Start", "End", "Title", "Visual Direction", "Screen Text"])
            for scene in scenes:
                writer.writerow([
                    scene.get("index"), scene.get("start"), scene.get("end"),
                    scene.get("title"), scene.get("visual_direction"), scene.get("screen_text"),
                ])
            atomic_write_text(csv_path, buffer.getvalue())

            md_path = project.dir / "visual_plan.md"
            lines = ["# Visual Plan\n"]
            for scene in scenes:
                lines.append(
                    f"## Scene {scene.get('index')}: {scene.get('title')} "
                    f"({scene.get('start')} - {scene.get('end')})"
                )
                lines.append(f"- **Visual:** {scene.get('visual_direction', '')}")
                lines.append(f"- **Screen:** {scene.get('screen_text', '')}")
                lines.append(f"- **Music:** {scene.get('music_note', '')}")
                lines.append(f"- **Retention:** {scene.get('retention_note', '')}")
                lines.append("")
            atomic_write_text(md_path, "\n".join(lines))

            return {"ok": True, "csv": str(csv_path), "md": str(md_path), "count": len(scenes)}
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))


@router.put("/{project_id:str}/scenes/{scene_index}")
async def update_scene(project_id: str, scene_index: int, data: dict[str, Any]):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")

    try:
        with jobs.project_mutation(path.name, "Редактирование сцены"):
            project = open_project(path, load_settings())
            if not project:
                raise HTTPException(404, "Project not found")
            scenes = project.read_json("scenes") or []
            for position, scene in enumerate(scenes):
                if scene.get("index") == scene_index:
                    unknown = sorted(set(data) - set(SceneSchema.model_fields))
                    if unknown:
                        raise HTTPException(422, {"message": "Unknown Scene fields", "fields": unknown})
                    try:
                        updated = SceneSchema.model_validate({**scene, **data}).model_dump()
                    except ValidationError as exc:
                        raise HTTPException(422, exc.errors())
                    scenes[position] = updated
                    project.write_json("scenes", scenes)
                    project.mark_stage_completed(
                        "scenes",
                        scenes,
                        input_hash=project.content_hash(_scene_input_payload(
                            project,
                            project.read_json("transcript_segments") or [],
                            project.effective_settings(),
                        )),
                        metadata={"edited": True},
                    )
                    return updated
            raise HTTPException(404, "Scene not found")
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
