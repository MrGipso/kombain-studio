from __future__ import annotations

import asyncio
import csv
import io
import re
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

_SCENE_TIMECODE_RE = re.compile(
    r"^(?P<hours>\d{2,}):(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)"
    r"(?:(?:[.,])(?P<fraction>\d{1,3}))?$"
)
_LEGACY_SCENE_TIMECODE_RE = re.compile(
    r"^(?P<minutes>\d+):(?P<seconds>[0-5]\d)"
    r"(?:(?:[.,])(?P<fraction>\d{1,3}))?$"
)


def _scene_timecode_milliseconds(
    value: Any,
    field: str,
    *,
    allow_legacy: bool = False,
) -> int:
    """Parse an editor timecode without the permissive pipeline fallbacks.

    Pipeline imports accept several legacy representations.  Manual edits are
    different: silently treating a typo as zero would corrupt every downstream
    timeline, so this boundary deliberately accepts only HH:MM:SS(.mmm).
    """
    if not isinstance(value, str):
        raise HTTPException(422, {"message": f"{field} must be a timecode string"})
    text = value.strip()
    match = _SCENE_TIMECODE_RE.fullmatch(text)
    if match:
        hours = int(match.group("hours"))
        minutes = int(match.group("minutes"))
    elif allow_legacy and (legacy := _LEGACY_SCENE_TIMECODE_RE.fullmatch(text)):
        match = legacy
        hours = 0
        minutes = int(match.group("minutes"))
    else:
        match = None
    if not match:
        raise HTTPException(
            422,
            {"message": f"{field} must use HH:MM:SS or HH:MM:SS.mmm"},
        )
    fraction = (match.group("fraction") or "").ljust(3, "0")
    return (
        hours * 3_600_000
        + minutes * 60_000
        + int(match.group("seconds")) * 1_000
        + int(fraction or "0")
    )


def _canonical_scene_timecode(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, fraction = divmod(remainder, 1_000)
    base = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{base}.{fraction:03d}" if fraction else base


def _scene_edit_value(field: str, value: Any) -> Any:
    """Normalize editor text solely for detecting whether a field changed."""
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if field in {"start", "end"}:
        normalized = normalized.replace(",", ".")
    return normalized


def _validate_scene_edit(
    scenes: list[dict[str, Any]],
    position: int,
    scene_index: int,
    updated: dict[str, Any],
    changed_fields: set[str],
) -> None:
    """Protect the ordered scene timeline before committing an editor patch."""
    updated["start"] = updated["start"].strip().replace(",", ".")
    updated["end"] = updated["end"].strip().replace(",", ".")
    updated["title"] = updated["title"].strip()
    indexes = [scene.get("index") for scene in scenes]
    if any(not isinstance(index, int) or isinstance(index, bool) for index in indexes):
        raise HTTPException(409, "Scene plan contains an invalid index; analyze scenes again")
    if len(indexes) != len(set(indexes)):
        raise HTTPException(409, "Scene plan contains duplicate indexes; analyze scenes again")
    if indexes != list(range(1, len(scenes) + 1)):
        raise HTTPException(409, "Scene plan indexes are out of order; analyze scenes again")
    if updated["index"] != scene_index:
        raise HTTPException(422, {"message": "Scene index is immutable"})

    start_ms = _scene_timecode_milliseconds(
        updated["start"], "start", allow_legacy="start" not in changed_fields,
    )
    end_ms = _scene_timecode_milliseconds(
        updated["end"], "end", allow_legacy="end" not in changed_fields,
    )
    updated["start"] = _canonical_scene_timecode(start_ms)
    updated["end"] = _canonical_scene_timecode(end_ms)
    if start_ms >= end_ms:
        raise HTTPException(422, {"message": "Scene start must be earlier than scene end"})

    if position:
        previous_end = _scene_timecode_milliseconds(
            scenes[position - 1].get("end"), "previous.end", allow_legacy=True,
        )
        if start_ms < previous_end:
            raise HTTPException(422, {"message": "Scene overlaps the previous scene"})
    if position + 1 < len(scenes):
        next_start = _scene_timecode_milliseconds(
            scenes[position + 1].get("start"), "next.start", allow_legacy=True,
        )
        if end_ms > next_start:
            raise HTTPException(422, {"message": "Scene overlaps the next scene"})

    if not updated["title"].strip():
        raise HTTPException(422, {"message": "Scene title cannot be empty"})


def _spreadsheet_safe(value: Any) -> Any:
    """Keep LLM/user-authored text from becoming a formula in CSV viewers."""
    if not isinstance(value, str):
        return value
    first_visible = value.lstrip(" \t\r\n")[:1]
    if value[:1] in {"\t", "\r", "\n"} or first_visible in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def _markdown_text(value: Any) -> str:
    """Render one untrusted scene field as inert, single-line Markdown text."""
    text = " ".join(str(value or "").splitlines()).strip()
    for marker in ("\\", "`", "*", "_", "[", "]", "<", ">", "#", "|"):
        text = text.replace(marker, "\\" + marker)
    return text


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
            loop.call_soon_threadsafe(sse.emit_to, sub_id, "log", {"message": msg})

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
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "done", {"scenes": scenes})
            except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "error", {"message": str(exc)})
            except Exception as exc:
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "error", {"message": str(exc)})

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
                writer.writerow([_spreadsheet_safe(value) for value in [
                    scene.get("index"), scene.get("start"), scene.get("end"),
                    scene.get("title"), scene.get("visual_direction"), scene.get("screen_text"),
                ]])
            atomic_write_text(csv_path, buffer.getvalue())

            md_path = project.dir / "visual_plan.md"
            lines = ["# Visual Plan\n"]
            for scene in scenes:
                lines.append(
                    f"## Scene {_markdown_text(scene.get('index'))}: {_markdown_text(scene.get('title'))} "
                    f"({_markdown_text(scene.get('start'))} - {_markdown_text(scene.get('end'))})"
                )
                lines.append(f"- **Visual:** {_markdown_text(scene.get('visual_direction', ''))}")
                lines.append(f"- **Screen:** {_markdown_text(scene.get('screen_text', ''))}")
                lines.append(f"- **Music:** {_markdown_text(scene.get('music_note', ''))}")
                lines.append(f"- **Retention:** {_markdown_text(scene.get('retention_note', ''))}")
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
            previous_scene_state = project.stage_states().get("scenes", {})
            for position, scene in enumerate(scenes):
                if scene.get("index") == scene_index:
                    unknown = sorted(set(data) - set(SceneSchema.model_fields))
                    if unknown:
                        raise HTTPException(422, {"message": "Unknown Scene fields", "fields": unknown})
                    try:
                        updated = SceneSchema.model_validate({**scene, **data}).model_dump()
                    except ValidationError as exc:
                        raise HTTPException(422, exc.errors())
                    changed_fields = {
                        field
                        for field, value in data.items()
                        if _scene_edit_value(field, value)
                        != _scene_edit_value(field, scene.get(field))
                    }
                    _validate_scene_edit(
                        scenes, position, scene_index, updated, changed_fields,
                    )
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
                    if previous_scene_state.get("status") == "stale":
                        project.invalidate_stage(
                            "scenes",
                            reason=(
                                previous_scene_state.get("error")
                                or "manual edit preserved stale scene inputs"
                            ),
                        )
                    elif previous_scene_state.get("status") == "failed":
                        project.mark_stage_failed(
                            "scenes",
                            str(previous_scene_state.get("error") or "scene analysis failed"),
                            input_hash=previous_scene_state.get("input_hash"),
                            metadata={"edited": True, "preserved_status": "failed"},
                        )
                    return updated
            raise HTTPException(404, "Scene not found")
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
