from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse

from core.config import load_settings
from core.project import open_project, safe_project_path
from core.transcribe import run_transcription

from backend.services.sse_manager import SSEManager
from backend.services import jobs, runlog

router = APIRouter()
sse = SSEManager()

AUDIO_UPLOAD_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
MAX_AUDIO_UPLOAD_BYTES = 500 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


@router.post("/{project_id:str}/upload-audio")
async def upload_audio(project_id: str, file: UploadFile = File(...)):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    voice_dir = project.dir / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)

    # Guard against path traversal in the client-supplied filename.
    safe_name = Path(file.filename or "audio").name
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(400, "Invalid filename")
    extension = Path(safe_name).suffix.lower()
    if extension not in AUDIO_UPLOAD_EXTENSIONS:
        raise HTTPException(400, f"Unsupported audio format: {extension or '(no extension)'}")
    dest = voice_dir / safe_name

    # Stream into a same-directory temporary file. A failed/interrupted upload
    # never replaces a previously valid recording and never leaves a partial
    # file that whisper could pick up.
    total = 0
    fd, temporary_name = tempfile.mkstemp(dir=str(voice_dir), prefix=".upload-", suffix=".part")
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as out:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_AUDIO_UPLOAD_BYTES:
                    raise HTTPException(413, "Audio file too large (max 500 MB)")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if total == 0:
            raise HTTPException(400, "Audio file is empty")
        try:
            with jobs.project_mutation(project.dir.name, "Загрузка аудио"):
                # Uploading the body does not affect pipeline inputs: the temp
                # suffix is deliberately ignored by transcription. Commit the
                # completed file and invalidate its consumers atomically with
                # respect to every other in-process project mutation.
                current_project = open_project(path, load_settings())
                if not current_project:
                    raise HTTPException(404, "Project not found")
                dest = current_project.voice_dir / safe_name
                os.replace(temporary, dest)
                current_project.invalidate_stage(
                    "transcript", reason=f"audio upload changed: {safe_name}",
                )
        except jobs.ProjectBusy as exc:
            raise HTTPException(409, str(exc))
    finally:
        temporary.unlink(missing_ok=True)
        await file.close()

    return {"path": str(dest), "filename": safe_name}


@router.post("/{project_id:str}/transcribe")
async def transcribe(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    audio_files = sorted(
        p for p in (project.dir / "voice").iterdir()
        if p.suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
    ) if (project.dir / "voice").exists() else []

    if not audio_files:
        raise HTTPException(400, "No audio file in project voice/ directory")

    async def event_stream():
        sub_id, q = sse.subscribe(project.dir.name)
        loop = asyncio.get_event_loop()

        def log_handler(msg: str):
            loop.call_soon_threadsafe(sse.emit, project.dir.name, "log", {"message": msg})

        def run_sync():
            try:
                with jobs.project_job(project.dir.name, "Транскрипция", resource="whisper") as cancel_event:
                    current_project = open_project(path, load_settings())
                    if not current_project:
                        raise RuntimeError("Project not found")
                    current_audio_files = sorted(
                        candidate
                        for candidate in current_project.voice_dir.iterdir()
                        if candidate.suffix.lower() in AUDIO_UPLOAD_EXTENSIONS
                    ) if current_project.voice_dir.exists() else []
                    if not current_audio_files:
                        raise ValueError("No audio file in project voice/ directory")
                    settings_dict = current_project.effective_settings()
                    try:
                        result = run_transcription(
                            current_project.dir, current_audio_files[0], settings_dict,
                            jobs.checking_logger(
                                runlog.tee(current_project.dir, "Транскрипция", log_handler),
                                cancel_event,
                            ),
                            cancel_check=lambda: jobs.check(cancel_event),
                        )
                        jobs.check(cancel_event)
                        current_project.write_text("transcript", result["text"])
                        current_project.write_json("transcript_segments", result["segments"])
                        if result.get("language"):
                            current_project.metadata["detected_language"] = result["language"]
                            current_project.save_meta()
                        current_project.mark_stage_completed(
                            "transcript",
                            {"text": result["text"], "segments": result["segments"]},
                            input_hash=current_project.content_hash({
                                "audio": current_project.content_hash(current_audio_files[0]),
                                "settings": {
                                    key: settings_dict.get(key)
                                    for key in ("whisper_model", "whisper_language", "whisper_cli", "ffmpeg")
                                },
                            }),
                            metadata={"language": result.get("language", "")},
                        )
                    except jobs.JobCancelled:
                        raise
                    except Exception as exc:
                        current_project.mark_stage_failed("transcript", str(exc))
                        raise
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "done", {
                    "text": result["text"],
                    "segments": result["segments"],
                    "json_path": result["json_path"],
                    "srt_path": result["srt_path"],
                    "language": result.get("language", ""),
                })
            except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "error", {"message": str(exc)})
            except Exception as exc:
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "error", {"message": str(exc)})

        loop.run_in_executor(None, run_sync)

        async for msg in sse.generator(sub_id):
            yield msg

    return StreamingResponse(event_stream(), media_type="text/event-stream")
