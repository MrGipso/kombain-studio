from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from core.config import load_settings
from core.project import open_project, safe_project_path, is_within
from backend.services import jobs

router = APIRouter()

EXCLUDED_STATUSES = {"skipped", "rejected"}

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
}


class TimelineSettings(BaseModel):
    voice_start: str = "00:00:00"


def _media_url(path: Path | str) -> str:
    return f"/api/media/{quote(str(path))}"


def _resolve_project(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
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
    project.write_json("timeline", settings)


@router.get("/media/{file_path:path}")
async def serve_media(file_path: str):
    path = Path(file_path).resolve()
    # Confine media serving to the projects root so a crafted URL cannot read
    # arbitrary files on disk (e.g. /api/media/C:/Users/.../secret).
    projects_root = Path(load_settings().output_base or "projects").resolve()
    if not is_within(path, projects_root):
        raise HTTPException(403, "Forbidden")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "File not found")

    # The old endpoint allowed every file under output_base, including cache,
    # SQLite and JSON metadata. Require an actual project directory and a
    # browser-consumable media extension. Absolute paths remain accepted for
    # compatibility with legacy boards, but never become an arbitrary file API.
    try:
        relative = path.relative_to(projects_root)
    except ValueError:
        raise HTTPException(403, "Forbidden")
    if len(relative.parts) < 2:
        raise HTTPException(403, "Forbidden")
    project_dir = projects_root / relative.parts[0]
    if relative.parts[0].startswith(".") or not (project_dir / ".project.json").is_file():
        raise HTTPException(403, "Forbidden")
    ext = path.suffix.lower()
    media_type = MEDIA_TYPES.get(ext)
    if media_type is None:
        raise HTTPException(403, "Unsupported media type")
    return FileResponse(str(path), media_type=media_type, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


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

    # Preview only an export that reached the stage commit. A raw MP4 may be a
    # cancelled render, a failed technical-QC candidate, or an orphan left by a
    # crash and must not be presented as the latest delivery.
    export_path: Path | None = None
    export_state = project.stage_state("export")
    export_metadata = (
        export_state.get("metadata")
        if isinstance(export_state.get("metadata"), dict)
        else {}
    )
    if export_state.get("status") == "completed":
        configured = Path(str(export_metadata.get("path", "") or ""))
        if configured.name:
            if not configured.is_absolute():
                configured = project.exports_dir / configured
            if (
                configured.suffix.lower() == ".mp4"
                and configured.is_file()
                and is_within(configured, project.exports_dir)
            ):
                export_path = configured.resolve()
        if export_path is None and not export_metadata and project.exports_dir.exists():
            # Legacy projects had no committed export metadata.
            export_path = _first_existing(
                sorted(
                    project.exports_dir.glob("*.mp4"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            )

    items = []
    for index, card in enumerate(cards):
        if str(card.get("status", "")).lower() in EXCLUDED_STATUSES:
            continue
        media_path = _first_existing(
            [
                card.get("video_output", ""),
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
    resolved = _resolve_project(project_id)
    try:
        with jobs.project_mutation(resolved.dir.name, "Изменение таймлайна"):
            # Re-open after acquiring the project lock so both the timeline
            # document and its manifest hash are derived from one fresh state.
            project = _resolve_project(project_id)
            data = {"voice_start": settings.voice_start or "00:00:00"}
            _write_timeline_settings(project, data)
            cards = project.read_json("asset_board") or []
            project.mark_stage_completed(
                "timeline",
                {"settings": data, "cards": cards},
                input_hash=project.content_hash(cards),
            )
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
    return data
