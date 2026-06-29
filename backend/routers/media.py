from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.config import load_settings
from core.project import open_project

router = APIRouter()


class TimelineSettings(BaseModel):
    voice_start: str = "00:00:00"


def _media_url(path: Path | str) -> str:
    return f"/api/media/{quote(str(path))}"


def _resolve_project(project_id: str):
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id
    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _first_existing(paths: list[str | Path]) -> Path | None:
    for value in paths:
        if not value:
            continue
        path = Path(value)
        if path.exists() and path.is_file():
            return path
    return None


def _media_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".mp4", ".webm", ".mov", ".mkv"}:
        return "video"
    if ext in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}:
        return "audio"
    if ext in {".png", ".jpg", ".jpeg", ".webp"}:
        return "image"
    return "file"


def _timeline_settings_path(project) -> Path:
    return project.dir / "08_timeline.json"


def _read_timeline_settings(project) -> dict:
    path = _timeline_settings_path(project)
    if not path.exists():
        return {"voice_start": "00:00:00"}
    try:
        data = __import__("json").loads(path.read_text(encoding="utf-8"))
        return {"voice_start": data.get("voice_start", "00:00:00")}
    except Exception:
        return {"voice_start": "00:00:00"}


def _write_timeline_settings(project, settings: dict) -> None:
    path = _timeline_settings_path(project)
    path.write_text(
        __import__("json").dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@router.get("/media/{file_path:path}")
async def serve_media(file_path: str):
    path = Path(file_path)
    if not path.exists():
        raise HTTPException(404, "File not found")

    ext = path.suffix.lower()
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
    }
    media_type = media_types.get(ext, "application/octet-stream")
    return FileResponse(str(path), media_type=media_type)


@router.get("/projects/{project_id:str}/timeline-media")
async def timeline_media(project_id: str):
    project = _resolve_project(project_id)
    cards = project.read_json("asset_board") or []
    timeline_settings = _read_timeline_settings(project)

    voice_path = _first_existing(
        sorted(
            [
                p for p in project.voice_dir.iterdir()
                if p.suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if project.voice_dir.exists()
        else []
    )

    export_path = _first_existing(
        sorted(
            [p for p in project.exports_dir.glob("*.mp4")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if project.exports_dir.exists()
        else []
    )

    items = []
    for index, card in enumerate(cards):
        media_path = _first_existing(
            [
                card.get("video_output", ""),
                card.get("remotion_output", ""),
                card.get("image_output", ""),
            ]
        )
        poster_path = _first_existing([card.get("image_output", "")])
        if not media_path:
            continue

        items.append(
            {
                "card_index": index,
                "shot_index": card.get("shot_index", index + 1),
                "scene_index": card.get("scene_index", 0),
                "start": card.get("start", ""),
                "end": card.get("end", ""),
                "title": card.get("description") or card.get("screen_text") or card.get("scene_title") or "",
                "status": card.get("status", ""),
                "approved": bool(card.get("approved", False)),
                "route": card.get("route", ""),
                "visual_type": card.get("visual_type", ""),
                "media_type": _media_kind(media_path),
                "path": str(media_path),
                "url": _media_url(media_path),
                "poster_url": _media_url(poster_path) if poster_path else "",
                "duration": None,
            }
        )

    return {
        "project_dir": str(project.dir),
        "voice_path": str(voice_path) if voice_path else "",
        "voice_url": _media_url(voice_path) if voice_path else "",
        "voice_start": timeline_settings.get("voice_start", "00:00:00"),
        "export_path": str(export_path) if export_path else "",
        "export_url": _media_url(export_path) if export_path else "",
        "items": items,
    }


@router.put("/projects/{project_id:str}/timeline-settings")
async def save_timeline_settings(project_id: str, settings: TimelineSettings):
    project = _resolve_project(project_id)
    data = {"voice_start": settings.voice_start or "00:00:00"}
    _write_timeline_settings(project, data)
    return data
