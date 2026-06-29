from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from core.config import load_settings
from core.project import open_project
from core.export import export_video
from core.validation import write_validation_report

from backend.services.sse_manager import SSEManager

router = APIRouter()
sse = SSEManager()


@router.post("/{project_id:str}/validate")
async def validate(project_id: str):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    try:
        report = write_validation_report(project)
        return report
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/{project_id:str}/export")
async def export(project_id: str):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    ffmpeg = project.settings.ffmpeg or "ffmpeg"

    async def event_stream():
        sub_id, q = sse.subscribe(project.dir.name)
        loop = asyncio.get_event_loop()

        def log_handler(msg: str):
            loop.call_soon_threadsafe(sse.emit, project.dir.name, "log", {"message": msg})

        def run_sync():
            try:
                result = export_video(
                    project_dir=project.dir,
                    ffmpeg_path=ffmpeg,
                    log_callback=log_handler,
                )
                if result:
                    loop.call_soon_threadsafe(sse.emit, project.dir.name, "done", {"path": str(result)})
                else:
                    loop.call_soon_threadsafe(sse.emit, project.dir.name, "error", {"message": "Export failed"})
            except Exception as exc:
                loop.call_soon_threadsafe(sse.emit, project.dir.name, "error", {"message": str(exc)})

        loop.run_in_executor(None, run_sync)

        async for msg in sse.generator(sub_id):
            yield msg

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/{project_id:str}/download")
async def download(project_id: str):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id

    exports_dir = path / "exports"
    if not exports_dir.exists():
        raise HTTPException(404, "No exports found")

    mp4_files = sorted(exports_dir.glob("*.mp4"))
    if not mp4_files:
        raise HTTPException(404, "No MP4 exports found")

    latest = mp4_files[-1]
    return FileResponse(
        str(latest),
        media_type="video/mp4",
        filename=latest.name,
    )
