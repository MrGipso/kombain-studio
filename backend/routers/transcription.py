from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse

from core.config import load_settings
from core.project import atomic_write_text, open_project, safe_project_path
from core.transcribe import run_transcription

from backend.services.sse_manager import SSEManager
from backend.services import jobs, runlog

router = APIRouter()
sse = SSEManager()

AUDIO_UPLOAD_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
MAX_AUDIO_UPLOAD_BYTES = 500 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
TRANSCRIPTION_SOURCE_FILE = "01_transcription_source.json"


def _resolve_project(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _audio_files(project) -> list[Path]:
    return sorted(
        [
            path
            for path in project.voice_dir.iterdir()
            if path.is_file() and path.suffix.lower() in AUDIO_UPLOAD_EXTENSIONS
        ],
        key=lambda item: (item.name.casefold() != "narration.wav", item.name.casefold()),
    ) if project.voice_dir.exists() else []


def _selection_path(project) -> Path:
    return project.dir / TRANSCRIPTION_SOURCE_FILE


def _read_selected_audio(project) -> Path | None:
    path = _selection_path(project)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        filename = Path(str(data.get("filename") or "")).name
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not filename or filename in {".", ".."}:
        return None
    candidate = project.voice_dir / filename
    if candidate.is_file() and candidate.suffix.lower() in AUDIO_UPLOAD_EXTENSIONS:
        return candidate
    return None


def _preferred_audio(project) -> Path | None:
    return _read_selected_audio(project) or next(iter(_audio_files(project)), None)


def _select_audio(project, audio_path: Path, *, origin: str) -> None:
    if audio_path.parent.resolve() != project.voice_dir.resolve():
        raise ValueError("Audio source must be inside the project voice directory")
    if not audio_path.is_file() or audio_path.suffix.lower() not in AUDIO_UPLOAD_EXTENSIONS:
        raise ValueError("Audio source is missing or unsupported")
    atomic_write_text(
        _selection_path(project),
        json.dumps(
            {"filename": audio_path.name, "origin": origin},
            ensure_ascii=False,
            indent=2,
        ),
    )


def _source_payload(project, preferred_filename: str = "") -> dict[str, Any]:
    selected = _read_selected_audio(project)
    requested = project.voice_dir / Path(preferred_filename).name if preferred_filename else None
    if requested is not None:
        audio = (
            requested
            if requested.is_file() and requested.suffix.lower() in AUDIO_UPLOAD_EXTENSIONS
            else None
        )
    else:
        audio = selected or next(iter(_audio_files(project)), None)
    if audio is None:
        return {
            "available": False,
            "filename": "",
            "path": "",
            "url": "",
            "selected": False,
            "origin": "",
        }
    origin = "omnivoice" if audio.name.casefold() == "narration.wav" else "upload"
    if selected is not None and selected.resolve() == audio.resolve():
        try:
            metadata = json.loads(_selection_path(project).read_text(encoding="utf-8"))
            origin = str(metadata.get("origin") or origin)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return {
        "available": True,
        "filename": audio.name,
        "path": str(audio),
        "url": f"/api/media/{quote(str(audio))}",
        "selected": selected is not None and selected.resolve() == audio.resolve(),
        "origin": origin,
    }


@router.get("/{project_id:str}/transcription-source")
async def get_transcription_source(project_id: str):
    return _source_payload(_resolve_project(project_id))


@router.get("/{project_id:str}/transcription-source/narration")
async def get_narration_source(project_id: str):
    return _source_payload(_resolve_project(project_id), "narration.wav")


@router.post("/{project_id:str}/transcription-source/narration")
async def select_narration_for_transcription(project_id: str):
    project = _resolve_project(project_id)
    try:
        with jobs.project_mutation(project.dir.name, "Передача озвучки в транскрипцию"):
            current = _resolve_project(project_id)
            narration = current.voice_dir / "narration.wav"
            if not narration.is_file():
                raise ValueError("Сначала создайте озвучку OmniVoice")
            previous = _read_selected_audio(current)
            _select_audio(current, narration, origin="omnivoice")
            if previous is None or previous.resolve() != narration.resolve():
                current.invalidate_stage(
                    "transcript",
                    reason="transcription source changed to voice/narration.wav",
                )
            return _source_payload(current)
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{project_id:str}/upload-audio")
async def upload_audio(project_id: str, file: UploadFile = File(...)):
    project = _resolve_project(project_id)
    path = project.dir

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
                _select_audio(current_project, dest, origin="upload")
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
    project = _resolve_project(project_id)
    path = project.dir
    if _preferred_audio(project) is None:
        raise HTTPException(400, "No audio file in project voice/ directory")

    async def event_stream():
        sub_id, q = sse.subscribe(project.dir.name)
        loop = asyncio.get_event_loop()

        def log_handler(msg: str):
            loop.call_soon_threadsafe(sse.emit_to, sub_id, "log", {"message": msg})

        def run_sync():
            try:
                with jobs.project_job(project.dir.name, "Транскрипция", resource="whisper") as cancel_event:
                    current_project = open_project(path, load_settings())
                    if not current_project:
                        raise RuntimeError("Project not found")
                    current_audio = _preferred_audio(current_project)
                    if current_audio is None:
                        raise ValueError("No audio file in project voice/ directory")
                    settings_dict = current_project.effective_settings()
                    try:
                        result = run_transcription(
                            current_project.dir, current_audio, settings_dict,
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
                                "audio": current_project.content_hash(current_audio),
                                "settings": {
                                    key: settings_dict.get(key)
                                    for key in ("whisper_model", "whisper_language", "whisper_cli", "ffmpeg")
                                },
                            }),
                            metadata={
                                "language": result.get("language", ""),
                                "audio_source": current_audio.name,
                            },
                        )
                    except jobs.JobCancelled:
                        raise
                    except Exception as exc:
                        current_project.mark_stage_failed("transcript", str(exc))
                        raise
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "done", {
                    "text": result["text"],
                    "segments": result["segments"],
                    "json_path": result["json_path"],
                    "srt_path": result["srt_path"],
                    "language": result.get("language", ""),
                })
            except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "error", {"message": str(exc)})
            except Exception as exc:
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "error", {"message": str(exc)})

        loop.run_in_executor(None, run_sync)

        async for msg in sse.generator(sub_id):
            yield msg

    return StreamingResponse(event_stream(), media_type="text/event-stream")
