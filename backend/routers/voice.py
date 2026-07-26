"""Voice library, reference cloning intake and OmniVoice rendering."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
import wave
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.services import jobs, runlog
from backend.services.sse_manager import SSEManager
from core.config import ROOT, load_settings
from core.omnivoice_tts import AUDIO_EXTENSIONS, OmniVoiceError, discover_voice_profiles, render_plan
from core.project import atomic_write_text, open_project, safe_project_path
from core.script_workflow import assess_script_quality, build_voice_plan, migrate_script
from core.transcribe import run_transcription


router = APIRouter()
sse = SSEManager()
MAX_REFERENCE_BYTES = 100 * 1024 * 1024


def _resolve_project(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _profiles(project_dir: Path) -> list[dict[str, Any]]:
    return discover_voice_profiles(project_dir, ROOT / "voices")


def _public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in profile.items() if key != "reference_audio"}


@router.get("/{project_id:str}/voices")
async def list_voices(project_id: str):
    project = _resolve_project(project_id)
    return [_public_profile(profile) for profile in _profiles(project.dir)]


async def _read_reference_upload(file: UploadFile) -> tuple[str, str, bytes]:
    safe_name = Path(file.filename or "reference.wav").name
    extension = Path(safe_name).suffix.lower()
    if extension not in AUDIO_EXTENSIONS:
        raise HTTPException(400, "Неподдерживаемый формат аудиореференса")
    content = await file.read(MAX_REFERENCE_BYTES + 1)
    await file.close()
    if not content:
        raise HTTPException(400, "Аудиореференс пуст")
    if len(content) > MAX_REFERENCE_BYTES:
        raise HTTPException(413, "Аудиореференс слишком большой (максимум 100 МБ)")
    return safe_name, extension, content


@router.post("/{project_id:str}/voices/reference/transcribe")
async def transcribe_reference_voice(project_id: str, file: UploadFile = File(...)):
    project = _resolve_project(project_id)
    safe_name, extension, content = await _read_reference_upload(file)
    try:
        def run():
            job = jobs.project_job(project.dir.name, "Расшифровка голосового референса", resource="whisper")
            with job as cancel_event:
                current = _resolve_project(project_id)
                voices_dir = current.dir / "voices"
                voices_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=str(voices_dir), prefix=".reference-transcribe-") as temp_name:
                    temp_root = Path(temp_name)
                    audio_path = temp_root / f"reference{extension}"
                    audio_path.write_bytes(content)
                    with runlog.run(
                        current.dir,
                        "Расшифровка голосового референса",
                        run_id=job.job_id,
                        job_id=job.job_id,
                        fields={"operation": "voice.reference.transcribe", "filename": safe_name},
                    ) as report:
                        result = run_transcription(
                            temp_root / "work",
                            audio_path,
                            current.effective_settings(),
                            log_callback=runlog.tee(
                                current.dir,
                                "Расшифровка голосового референса",
                                None,
                                run_id=job.job_id,
                                job_id=job.job_id,
                            ),
                            cancel_check=lambda: jobs.check(cancel_event),
                        )
                        text = str(result.get("text") or "").strip()
                        if not text:
                            raise ValueError("Whisper не распознал речь в аудиореференсе")
                        report.outcome(
                            "Референс расшифрован",
                            characters=len(text),
                            language=result.get("language", ""),
                        )
                        return {
                            "text": text,
                            "language": result.get("language", ""),
                            "segments": result.get("segments", []),
                        }
        return await asyncio.to_thread(run)
    except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except (OSError, RuntimeError) as exc:
        raise HTTPException(502, f"Расшифровка референса: {exc}")


@router.post("/{project_id:str}/voices/reference")
async def add_reference_voice(
    project_id: str,
    file: UploadFile = File(...),
    display_name: str = Form("Мой голос"),
    language: str = Form("ru"),
    transcript: str = Form(""),
):
    project = _resolve_project(project_id)
    _safe_name, extension, content = await _read_reference_upload(file)
    transcript = transcript.strip()
    if not transcript:
        raise HTTPException(400, "Сначала расшифруйте аудиореференс или введите его точный текст")

    try:
        with jobs.project_mutation(project.dir.name, "Добавление голосового референса"):
            current = _resolve_project(project_id)
            profile_id = f"voice_{uuid.uuid4().hex[:12]}"
            profile_dir = current.dir / "voices" / profile_id
            profile_dir.mkdir(parents=True, exist_ok=False)
            reference = profile_dir / f"reference{extension}"
            fd, temporary_name = tempfile.mkstemp(dir=str(profile_dir), prefix=".reference-", suffix=".part")
            os.close(fd)
            temporary = Path(temporary_name)
            duration: float | None = None
            try:
                temporary.write_bytes(content)
                if extension == ".wav":
                    try:
                        with wave.open(str(temporary), "rb") as stream:
                            duration = stream.getnframes() / float(stream.getframerate())
                    except (wave.Error, OSError):
                        raise HTTPException(400, "Некорректный WAV-файл")
                os.replace(temporary, reference)
                metadata = {
                    "id": profile_id,
                    "type": "clone",
                    "display_name": display_name.strip()[:80] or "Мой голос",
                    "language": language.strip()[:20] or "ru",
                    "description": "Пользовательский голос по аудиореференсу",
                    "reference_audio": reference.name,
                    "reference_transcript": transcript,
                    "calibrated_wpm": 135,
                    "duration_sec": round(duration, 3) if duration is not None else None,
                }
                atomic_write_text(profile_dir / "profile.json", json.dumps(metadata, ensure_ascii=False, indent=2))
            finally:
                temporary.unlink(missing_ok=True)
            profiles = {profile["id"]: profile for profile in _profiles(current.dir)}
            return _public_profile(profiles[profile_id]) | {"duration_sec": metadata["duration_sec"]}
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))


def _script_voice_notices(
    script: dict[str, Any],
    quality: dict[str, Any],
) -> list[str]:
    if script.get("mode") != "rewrite":
        return []
    notices: list[str] = []
    agent = script.get("agent") if isinstance(script.get("agent"), dict) else {}
    issues = [item for item in (agent.get("issues") or []) if isinstance(item, dict)]
    if issues:
        notices.append(f"редакторских замечаний: {len(issues)}")
    status = str(agent.get("status") or "idle")
    if status in {"idle", "running", "needs_review", "failed", "cancelled"}:
        notices.append(f"статус проверки: {agent.get('stage') or status}")
    if quality.get("status") != "ready":
        notices.append(
            f"объём {quality.get('actual_words', 0)}/{quality.get('target_words', 0)} слов"
        )
    return notices


class RenderVoiceRequest(BaseModel):
    profile_id: str
    speed: float = Field(1.0, ge=0.7, le=1.3)
    pause_scale: float = Field(1.0, ge=0.25, le=3.0)
    num_step: int = Field(32, ge=8, le=64)
    guidance_scale: float = Field(2.0, ge=0.5, le=6.0)
    denoise: bool = True
    postprocess_output: bool = True
    device: Literal["auto", "cuda", "cpu", "mps"] = "auto"


def _render_voice_sync(
    project_id: str,
    req: RenderVoiceRequest,
    live_log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    project = _resolve_project(project_id)
    job = jobs.project_job(project.dir.name, "Озвучка OmniVoice", resource="tts")
    with job as cancel_event:
        current = _resolve_project(project_id)
        profiles = {profile["id"]: profile for profile in _profiles(current.dir)}
        profile = profiles.get(req.profile_id)
        if not profile:
            raise ValueError("Выбранный голос не найден")
        script_path = current.dir / "00_script.json"
        script = migrate_script(json.loads(script_path.read_text(encoding="utf-8")) if script_path.exists() else None)
        text = str(script.get("final_text") or "").strip()
        if not text:
            raise ValueError("Сначала подготовьте итоговый сценарий")
        quality = assess_script_quality(script)
        script["quality"] = quality
        notices = _script_voice_notices(script, quality)
        if live_log and notices:
            live_log("Сценарий: " + " · ".join(notices) + " Озвучка разрешена.")
        plan = build_voice_plan(text, speed=req.speed, pause_scale=req.pause_scale)
        plan["voice_profile_id"] = req.profile_id
        settings = load_settings()
        with runlog.run(
            current.dir, "Озвучка OmniVoice", run_id=job.job_id, job_id=job.job_id,
            fields={
                "operation": "tts.render",
                "voice_profile_id": req.profile_id,
                "num_step": req.num_step,
                "guidance_scale": req.guidance_scale,
                "speed": req.speed,
            },
        ) as report:
            log = runlog.tee(
                current.dir,
                "Озвучка OmniVoice",
                live_log,
                run_id=job.job_id,
                job_id=job.job_id,
            )
            log(
                f"Подготовлен план: {len(plan.get('chunks', []))} фрагментов; "
                f"голос — {profile.get('display_name', req.profile_id)}"
            )
            jobs.check(cancel_event)
            result = render_plan(
                plan, profile, current.voice_dir,
                cli=settings.omnivoice_cli, model=settings.omnivoice_model,
                generation={
                    "num_step": req.num_step,
                    "guidance_scale": req.guidance_scale,
                    "denoise": req.denoise,
                    "postprocess_output": req.postprocess_output,
                    "device": req.device,
                },
                log=log,
                cancel_check=lambda: jobs.check(cancel_event),
            )
            jobs.check(cancel_event)
            script.update({
                "voice_profile_id": req.profile_id,
                "estimated_wpm": int(profile.get("calibrated_wpm") or 135),
                "actual_audio_duration_sec": round(float(result["duration_sec"]), 3),
                "voice_source_revision": int(script.get("text_revision", 0) or 0),
                "voice_outdated": False,
                "voice_plan": plan,
                "revision": int(script.get("revision", 0) or 0) + 1,
            })
            atomic_write_text(script_path, json.dumps(script, ensure_ascii=False, indent=2))
            atomic_write_text(current.dir / "00_voice_plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
            current.invalidate_stage("transcript", reason="OmniVoice narration changed")
            report.outcome("Озвучка создана", duration_sec=result["duration_sec"])
            log(f"Готовая дорожка: {result['duration_sec']:.1f} сек")
            return {"ok": True, "script": script, **result}


@router.post("/{project_id:str}/tts/render")
async def render_voice(project_id: str, req: RenderVoiceRequest):
    try:
        return await asyncio.to_thread(_render_voice_sync, project_id, req)
    except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except (OmniVoiceError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"OmniVoice: {exc}")


@router.post("/{project_id:str}/tts/render-stream")
async def render_voice_stream(project_id: str, req: RenderVoiceRequest):
    project = _resolve_project(project_id)
    project_key = project.dir.name

    async def event_stream():
        sub_id, _queue = sse.subscribe(project_key)
        loop = asyncio.get_running_loop()

        def emit_log(message: str) -> None:
            loop.call_soon_threadsafe(sse.emit_to, sub_id, "log", {"message": message})

        def run_sync() -> None:
            try:
                emit_log("Запуск озвучки OmniVoice…")
                result = _render_voice_sync(project_id, req, live_log=emit_log)
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "done", result)
            except jobs.JobCancelled as exc:
                loop.call_soon_threadsafe(
                    sse.emit_to,
                    sub_id,
                    "error",
                    {"message": str(exc) or "Озвучка отменена", "code": "cancelled"},
                )
            except jobs.ProjectBusy as exc:
                loop.call_soon_threadsafe(
                    sse.emit_to, sub_id, "error", {"message": str(exc), "code": "project_busy"}
                )
            except Exception as exc:
                message = f"OmniVoice: {exc}" if isinstance(exc, OmniVoiceError) else str(exc)
                loop.call_soon_threadsafe(
                    sse.emit_to, sub_id, "error", {"message": message, "code": "failed"}
                )

        loop.run_in_executor(None, run_sync)
        async for message in sse.generator(sub_id):
            yield message

    return StreamingResponse(event_stream(), media_type="text/event-stream")
