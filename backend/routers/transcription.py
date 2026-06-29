from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse

from core.config import load_settings
from core.project import open_project

from backend.services.pipeline_orch import run_transcription
from backend.services.sse_manager import SSEManager

router = APIRouter()
sse = SSEManager()


@router.post("/{project_id:str}/upload-audio")
async def upload_audio(project_id: str, file: UploadFile = File(...)):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    voice_dir = project.dir / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    dest = voice_dir / file.filename
    content = await file.read()
    dest.write_bytes(content)

    return {"path": str(dest), "filename": file.filename}


@router.post("/{project_id:str}/transcribe")
async def transcribe(project_id: str):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    audio_files = sorted(
        p for p in (project.dir / "voice").iterdir()
        if p.suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
    ) if (project.dir / "voice").exists() else []

    if not audio_files:
        raise HTTPException(400, "No audio file in project voice/ directory")

    settings_dict = {f.name: getattr(project.settings, f.name) for f in project.settings.__dataclass_fields__.values()}

    async def event_stream():
        sub_id, q = sse.subscribe(project.dir.name)
        loop = asyncio.get_event_loop()

        def log_handler(msg: str):
            loop.call_soon_threadsafe(sse.emit, project.dir.name, "log", {"message": msg})

        def run_sync():
            try:
                result = run_transcription(
                    project.dir, audio_files[0], settings_dict, log_handler,
                )
                project.write_text("transcript", result["text"])
                project.write_json("transcript_segments", result["segments"])
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "done", {
                    "text": result["text"],
                    "segments": result["segments"],
                    "json_path": result["json_path"],
                    "srt_path": result["srt_path"],
                })
            except Exception as exc:
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "error", {"message": str(exc)})

        loop.run_in_executor(None, run_sync)

        async for msg in sse.generator(sub_id):
            yield msg

    return StreamingResponse(event_stream(), media_type="text/event-stream")
