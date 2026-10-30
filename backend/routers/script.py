"""Script stage (step 00): topic -> structure (hooks + beats) -> narration text.

Professional pipelines design retention BEFORE recording: the hook and the beat
structure are authored in the script, not reverse-engineered from a finished
voice-over. This router stores the script in <project>/00_script.json, drives
the LLM for structure/text, and exports 00_script.txt for the user to record.
Scene analysis later prefers authored beats over embedding segmentation (see
core/script_align.py).

Draft generation and expansion live in core/script_generation.py, the rewrite
agent in core/rewrite_agent.py and persistence in core/script_store.py; this
module adds HTTP, project locking, run logs and SSE around them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core import script_generation
from core.config import load_settings
from core.project import open_project, safe_project_path, atomic_write_text
from core.script_store import (
    SCRIPT_TEXT_FILE,
    VOICE_PLAN_FILE,
    read_script,
    write_script,
    write_source,
)
from core.script_workflow import (
    DEFAULT_WPM,
    apply_requested_duration,
    assess_script_quality,
    build_voice_plan,
    clean_subtitle_text,
    default_script,
    estimate_duration_sec,
    migrate_script,
    set_script_source,
    split_into_beats,
    word_count,
)
from core.youtube_subtitles import SubtitleImportError, download_subtitle, inspect_subtitles
from core.rewrite_agent import (
    RewriteAgentRun,
    agent_payload,
    agent_release_status,
    validate_rewrite,
)

from backend.services import jobs, runlog
from backend.services.sse_manager import SSEManager

router = APIRouter()
sse = SSEManager()

MAX_SCRIPT_UPLOAD_BYTES = 10 * 1024 * 1024
SCRIPT_UPLOAD_EXTENSIONS = {".txt", ".md", ".srt", ".vtt"}


def _resolve_project(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/{project_id:str}/script")
async def get_script(project_id: str):
    project = _resolve_project(project_id)
    return read_script(project.dir)


@router.put("/{project_id:str}/script")
async def save_script(project_id: str, data: dict[str, Any]):
    project = _resolve_project(project_id)
    try:
        with jobs.project_mutation(project.dir.name, "Редактирование сценария"):
            # Re-open and re-read after acquiring the shared lock. A separate
            # running_job() check has a TOCTOU gap and can also merge into a
            # stale script snapshot.
            current_project = _resolve_project(project_id)
            current = read_script(current_project.dir)
            previous_text = str(current.get("final_text") or "")
            previous_voice = str(current.get("voice_profile_id") or "")
            current.update({k: v for k, v in data.items() if k in default_script()})
            current = migrate_script(current)
            current["estimated_duration_sec"] = estimate_duration_sec(
                str(current.get("final_text") or ""), int(current.get("estimated_wpm") or DEFAULT_WPM)
            )
            if str(current.get("final_text") or "") != previous_text:
                current["voice_plan"] = {"status": "empty", "chunks": []}
                current["text_revision"] = int(current.get("text_revision", 0) or 0) + 1
                current["voice_outdated"] = current.get("actual_audio_duration_sec") is not None
                current["beats"] = split_into_beats(
                    str(current.get("final_text") or ""),
                    target_seconds=current.get("target_duration_sec"),
                    wpm=int(current.get("estimated_wpm") or DEFAULT_WPM),
                )
                current["agent"] = {
                    "status": "ready_with_warnings" if current.get("mode") == "rewrite" else "idle",
                    "stage": "Текст изменён вручную — проверка рекомендуется",
                    "progress": 0,
                    "iteration": 0,
                    "max_iterations": 1,
                    "score": 0,
                    "issues": [{
                        "id": "manual-edit-unvalidated",
                        "type": "manual_edit_unvalidated",
                        "severity": "high",
                        "block_index": None,
                        "message": "После ручного изменения сценарий рекомендуется проверить агентом.",
                        "evidence": "",
                        "suggested_action": "validate",
                    }] if current.get("mode") == "rewrite" else [],
                    "history": [],
                    "last_report_signature": "",
                    "last_checked_revision": -1,
                }
                current["revision"] = int(current.get("revision", 0) or 0) + 1
                if current.get("mode") == "rewrite":
                    existing_quality = current.get("quality") if isinstance(current.get("quality"), dict) else {}
                    total_beats = len(current.get("beats") or [])
                    current["quality"] = {
                        **existing_quality,
                        "total_beats": total_beats,
                        # A manual edit is an explicit author override; length
                        # remains mandatory, while plan coverage is accepted.
                        "completed_beats": total_beats,
                        "coverage_ratio": 1.0 if total_beats else 0.0,
                        "accepted_actual": False,
                    }
                    current["quality"] = assess_script_quality(current)
            elif str(current.get("voice_profile_id") or "") != previous_voice:
                current["voice_outdated"] = current.get("actual_audio_duration_sec") is not None
            write_script(current_project.dir, current)
            quality = current.get("quality") if isinstance(current.get("quality"), dict) else {}
            if str(current.get("final_text") or "").strip():
                current_project.mark_stage_completed("script", current, metadata={"edited": True})
            else:
                current_project.mark_stage_failed(
                    "script", "Итоговый сценарий пуст",
                    metadata={"edited": True, "quality": quality},
                )
            return current
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))


class TextImportRequest(BaseModel):
    mode: str = "ready"
    text: str
    target_minutes: int | None = None
    source_name: str = "Вставленный текст"


class YoutubeInspectRequest(BaseModel):
    url: str


class YoutubeImportRequest(BaseModel):
    url: str
    language: str
    kind: str = "manual"
    fallback_language: str = ""
    fallback_kind: str = ""
    target_minutes: int | None = None
    source_duration_sec: int | None = None


class ProcessScriptRequest(BaseModel):
    rewrite_style: str = "deep"
    target_minutes: int | None = None


class RewriteAgentRequest(ProcessScriptRequest):
    max_iterations: int = 1
    regenerate: bool = False


class VoicePlanRequest(BaseModel):
    speed: float = 1.0
    pause_scale: float = 1.0


@router.post("/{project_id:str}/script/import-text")
async def import_script_text(project_id: str, req: TextImportRequest):
    project = _resolve_project(project_id)
    try:
        with jobs.project_mutation(project.dir.name, "Импорт сценария"):
            current = _resolve_project(project_id)
            script = set_script_source(
                read_script(current.dir), req.text, mode=req.mode,
                source={"type": "text", "name": req.source_name, "url": "", "language": "ru", "caption_kind": ""},
                target_minutes=req.target_minutes,
            )
            write_source(current.dir, req.text, script["source"])
            write_script(current.dir, script)
            if script.get("final_text"):
                atomic_write_text(current.dir / SCRIPT_TEXT_FILE, script["final_text"])
            current.mark_stage_completed("script", script, metadata={"mode": req.mode, "imported": True})
            return script
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{project_id:str}/script/import-file")
async def import_script_file(
    project_id: str,
    file: UploadFile = File(...),
    mode: str = Form("ready"),
    target_minutes: int | None = Form(None),
):
    safe_name = Path(file.filename or "source.txt").name
    extension = Path(safe_name).suffix.lower()
    if extension not in SCRIPT_UPLOAD_EXTENSIONS:
        raise HTTPException(400, "Поддерживаются TXT, MD, SRT и VTT")
    content = await file.read(MAX_SCRIPT_UPLOAD_BYTES + 1)
    await file.close()
    if len(content) > MAX_SCRIPT_UPLOAD_BYTES:
        raise HTTPException(413, "Файл слишком большой (максимум 10 МБ)")
    try:
        raw = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "Файл должен быть в кодировке UTF-8")
    text = clean_subtitle_text(raw) if extension in {".srt", ".vtt"} else raw.strip()
    return await import_script_text(
        project_id,
        TextImportRequest(mode=mode, text=text, target_minutes=target_minutes, source_name=safe_name),
    )


@router.post("/{project_id:str}/script/youtube/inspect")
async def inspect_youtube(project_id: str, req: YoutubeInspectRequest):
    _resolve_project(project_id)
    try:
        settings = load_settings()
        return await asyncio.to_thread(
            inspect_subtitles,
            req.url,
            cookies_browser=settings.youtube_cookies_browser,
            retry_count=settings.youtube_retry_count,
            request_delay_sec=settings.youtube_request_delay_sec,
        )
    except SubtitleImportError as exc:
        raise HTTPException(exc.http_status, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{project_id:str}/script/youtube/import")
async def import_youtube(project_id: str, req: YoutubeImportRequest):
    project = _resolve_project(project_id)
    try:
        def run():
            job = jobs.project_job(project.dir.name, "Импорт субтитров YouTube", resource="network")
            with job as cancel_event:
                current = _resolve_project(project_id)
                source_dir = current.dir / "sources" / "youtube"
                jobs.check(cancel_event)
                settings = load_settings()
                subtitle = download_subtitle(
                    req.url,
                    req.language,
                    req.kind,
                    source_dir,
                    fallback_language=(
                        req.fallback_language if settings.youtube_fallback_original else ""
                    ),
                    fallback_kind=req.fallback_kind,
                    cookies_browser=settings.youtube_cookies_browser,
                    retry_count=settings.youtube_retry_count,
                    request_delay_sec=settings.youtube_request_delay_sec,
                )
                jobs.check(cancel_event)
                raw = subtitle.path.read_text(encoding="utf-8-sig", errors="replace")
                text = clean_subtitle_text(raw)
                script = set_script_source(
                    read_script(current.dir), text, mode="rewrite",
                    source={
                        "type": "youtube", "name": subtitle.path.name, "url": req.url,
                        "language": subtitle.language, "caption_kind": subtitle.kind,
                    },
                    target_minutes=req.target_minutes,
                    source_duration_sec=req.source_duration_sec,
                )
                write_source(current.dir, raw, script["source"])
                write_script(current.dir, script)
                current.mark_stage_completed("script", script, metadata={"mode": "rewrite", "youtube": True})
                return script
        return await asyncio.to_thread(run)
    except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
        raise HTTPException(409, str(exc))
    except SubtitleImportError as exc:
        raise HTTPException(exc.http_status, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{project_id:str}/script/process")
async def process_script(project_id: str, req: ProcessScriptRequest):
    project = _resolve_project(project_id)
    try:
        def run():
            job = jobs.project_job(project.dir.name, "Подготовка сценария", resource="llm")
            with job as cancel_event:
                current = _resolve_project(project_id)
                script = read_script(current.dir)
                previous_target = script.get("target_duration_sec")
                script = apply_requested_duration(script, req.target_minutes)
                if script.get("target_duration_sec") != previous_target:
                    write_script(current.dir, script)
                with runlog.run(
                    current.dir,
                    "Подготовка сценария",
                    run_id=job.job_id,
                    job_id=job.job_id,
                    fields={
                        "operation": "script.process",
                        "mode": script.get("mode"),
                        "target_duration_sec": script.get("target_duration_sec"),
                    },
                ) as report:
                    jobs.check(cancel_event)
                    result = script_generation.process_script(
                        current.dir,
                        script,
                        req.rewrite_style,
                        settings=load_settings(),
                        log=report.log,
                        cancel_check=lambda: jobs.check(cancel_event),
                    )
                    jobs.check(cancel_event)
                    quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
                    ready = result.get("mode") != "rewrite" or quality.get("status") == "ready"
                    current.mark_stage_completed(
                        "script",
                        result,
                        metadata={
                            "mode": result.get("mode"),
                            "quality": quality,
                            "has_notices": not ready,
                        },
                    )
                    report.outcome(
                        "Сценарий подготовлен" if ready else "Сценарий сохранён с замечаниями",
                        words=word_count(str(result.get("final_text") or "")),
                        duration_sec=result.get("estimated_duration_sec"),
                        quality_status=quality.get("status"),
                    )
                    return result
        return await asyncio.to_thread(run)
    except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Подготовка сценария: {exc}")


def _run_rewrite_agent_sync(
    project_id: str,
    req: RewriteAgentRequest,
    *,
    live_log=None,
    live_progress=None,
) -> dict[str, Any]:
    project = _resolve_project(project_id)
    agent = RewriteAgentRun(load_settings(), progress=live_progress)

    job = jobs.project_job(project.dir.name, "Агент-редактор сценария", resource="llm")
    try:
        with job as cancel_event:
            current = _resolve_project(project_id)
            with runlog.run(
                current.dir,
                "Агент-редактор сценария",
                run_id=job.job_id,
                job_id=job.job_id,
                fields={"operation": "script.agent", "max_iterations": agent.max_iterations},
            ) as lifecycle:
                def log(message: str) -> None:
                    lifecycle.log(message)
                    if live_log:
                        live_log(message)

                outcome = agent.run(
                    current,
                    rewrite_style=req.rewrite_style,
                    target_minutes=req.target_minutes,
                    regenerate=req.regenerate,
                    log=log,
                    cancel_check=lambda: jobs.check(cancel_event),
                )
                lifecycle.outcome(
                    outcome.message,
                    agent_status=outcome.status,
                    score=outcome.score,
                    blocking_issues=outcome.blocking_issues,
                    warnings=outcome.warnings,
                )
                return outcome.script
    except jobs.JobCancelled:
        agent.record_cancelled()
        raise
    except Exception:
        agent.record_failed()
        raise
    finally:
        agent.release_lm()


@router.post("/{project_id:str}/script/agent")
async def run_rewrite_agent(project_id: str, req: RewriteAgentRequest):
    try:
        return await asyncio.to_thread(_run_rewrite_agent_sync, project_id, req)
    except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Агент-редактор: {exc}")


@router.post("/{project_id:str}/script/agent-stream")
async def run_rewrite_agent_stream(project_id: str, req: RewriteAgentRequest):
    project = _resolve_project(project_id)
    project_key = project.dir.name

    async def event_stream():
        sub_id, _queue = sse.subscribe(project_key)
        loop = asyncio.get_running_loop()

        def emit_log(message: str) -> None:
            loop.call_soon_threadsafe(sse.emit_to, sub_id, "log", {"message": message})

        def emit_progress(stage: str, progress: int) -> None:
            loop.call_soon_threadsafe(
                sse.emit_to,
                sub_id,
                "progress",
                {"step": stage, "progress": progress},
            )

        def run_sync() -> None:
            try:
                result = _run_rewrite_agent_sync(
                    project_id,
                    req,
                    live_log=emit_log,
                    live_progress=emit_progress,
                )
                loop.call_soon_threadsafe(
                    sse.emit_to,
                    sub_id,
                    "done",
                    {"script": result, "agent": result.get("agent", {})},
                )
            except jobs.JobCancelled as exc:
                loop.call_soon_threadsafe(
                    sse.emit_to,
                    sub_id,
                    "error",
                    {"message": str(exc) or "Работа агента отменена", "code": "cancelled"},
                )
            except jobs.ProjectBusy as exc:
                loop.call_soon_threadsafe(
                    sse.emit_to,
                    sub_id,
                    "error",
                    {"message": str(exc), "code": "project_busy"},
                )
            except Exception as exc:
                loop.call_soon_threadsafe(
                    sse.emit_to,
                    sub_id,
                    "error",
                    {"message": str(exc), "code": "failed"},
                )

        loop.run_in_executor(None, run_sync)
        async for message in sse.generator(sub_id):
            yield message

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/{project_id:str}/script/expand")
async def expand_script(project_id: str):
    project = _resolve_project(project_id)
    try:
        def run():
            job = jobs.project_job(project.dir.name, "Доведение сценария до цели", resource="llm")
            with job as cancel_event:
                current = _resolve_project(project_id)
                with runlog.run(
                    current.dir,
                    "Доведение сценария до цели",
                    run_id=job.job_id,
                    job_id=job.job_id,
                    fields={"operation": "script.expand"},
                ) as report:
                    result = script_generation.expand_script(
                        current.dir,
                        read_script(current.dir),
                        settings=load_settings(),
                        log=report.log,
                        cancel_check=lambda: jobs.check(cancel_event),
                    )
                    ready = result.get("quality", {}).get("status") == "ready"
                    current.mark_stage_completed(
                        "script",
                        result,
                        metadata={
                            "expanded": True,
                            "has_notices": not ready,
                            "quality": result.get("quality", {}),
                        },
                    )
                    report.outcome("Сценарий дополнен", quality_status=result.get("quality", {}).get("status"))
                    return result
        return await asyncio.to_thread(run)
    except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Доведение сценария: {exc}")


@router.post("/{project_id:str}/script/accept-actual")
async def accept_actual_duration(project_id: str):
    project = _resolve_project(project_id)
    try:
        with jobs.project_mutation(project.dir.name, "Принятие длительности сценария"):
            current = _resolve_project(project_id)
            script = read_script(current.dir)
            quality = assess_script_quality(script)
            if script.get("mode") != "rewrite" or not quality.get("actual_words"):
                raise ValueError("Нет готового рерайта для принятия")
            if float(quality.get("coverage_ratio") or 0) < 0.999:
                raise ValueError("Нельзя принять длительность: сначала завершите все смысловые блоки")
            actual_duration = estimate_duration_sec(
                str(script.get("final_text") or ""), int(script.get("estimated_wpm") or DEFAULT_WPM)
            )
            actual_words = int(quality["actual_words"])
            script["target_duration_sec"] = actual_duration
            script["target_minutes"] = max(1, round(actual_duration / 60))
            script["estimated_duration_sec"] = actual_duration
            script["quality"] = {
                **quality,
                "target_words": actual_words,
                "word_ratio": 1.0,
                "status": "ready",
                "accepted_actual": True,
            }
            report = validate_rewrite(script)
            notice_count = len(report.get("issues") or [])
            status, stage = agent_release_status(report)
            agent_payload(
                script,
                status=status,
                stage=stage,
                progress=100,
                report=report,
            )
            script["revision"] = int(script.get("revision", 0) or 0) + 1
            write_script(current.dir, script)
            current.mark_stage_completed("script", script, metadata={"accepted_actual_duration": True})
            return script
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{project_id:str}/script/voice-plan")
async def create_voice_plan(project_id: str, req: VoicePlanRequest):
    project = _resolve_project(project_id)
    try:
        with jobs.project_mutation(project.dir.name, "План озвучки"):
            current = _resolve_project(project_id)
            script = read_script(current.dir)
            text = str(script.get("final_text") or "").strip()
            if not text:
                raise HTTPException(400, "Сначала подготовьте итоговый сценарий")
            plan = build_voice_plan(text, speed=req.speed, pause_scale=req.pause_scale)
            plan["voice_profile_id"] = str(script.get("voice_profile_id") or "")
            script["voice_plan"] = plan
            script["revision"] = int(script.get("revision", 0) or 0) + 1
            atomic_write_text(current.dir / VOICE_PLAN_FILE, json.dumps(plan, ensure_ascii=False, indent=2))
            write_script(current.dir, script)
            current.invalidate_stage("transcript", reason="voice plan changed")
            return {"script": script, "plan": plan}
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))


def _structure_sync(pdir: Path, script: dict[str, Any]) -> dict[str, Any]:
    settings = load_settings()
    topic = script.get("topic", "").strip()
    if not topic:
        raise ValueError("Сначала укажи тему")
    minutes = int(script.get("target_minutes", 8) or 8)
    n_beats = max(5, min(16, round(minutes * 1.4)))
    prompt = (
        f"Ты сценарист документальных YouTube-роликов с высоким удержанием.\n"
        f"Тема ролика: {topic}\n"
        f"Целевая длительность: {minutes} минут.\n"
        + (f"Аудитория/заметки: {script.get('audience_notes')}\n" if script.get("audience_notes") else "")
        + (f"Фактура (проверенные факты, используй ТОЛЬКО их для конкретики):\n{script.get('facts')}\n" if script.get("facts") else "")
        + "\nСпроектируй структуру ролика ДО написания текста.\n"
        f"Верни JSON-объект: {{\"hooks\": [3 варианта], \"beats\": [~{n_beats} битов]}}.\n"
        "hooks — 3 разных варианта хука (первые 15-20 секунд): интрига, конкретика, обещание. "
        "Каждый hook — строка, готовый текст начитки 2-4 предложения. Без кликбейт-лжи.\n"
        "beats — смысловые биты повествования. Каждый бит: "
        "{\"title\": заголовок 3-6 слов, \"goal\": зачем бит зрителю и что удерживает (1 предложение), "
        "\"duration_sec\": целевая длительность в секундах, \"visual_hint\": что показывать (1 предложение), "
        "\"content\": план содержания — 2-4 тезиса через '; '}.\n"
        "Правила структуры: поворот или новая интрига каждые 2-3 минуты; конкретные цифры и примеры в goal/content; "
        "предпоследний бит — кульминация; последний — вывод с мостиком на следующий ролик. "
        "Сумма duration_sec ≈ целевой длительности. Русский язык. Только валидный JSON."
    )
    data = script_generation.lm_json(prompt, "Отвечай только валидным JSON. Не добавляй markdown.", settings)
    hooks = [str(h).strip() for h in data.get("hooks", []) if str(h).strip()][:3]
    beats = []
    for b in data.get("beats", []):
        if not isinstance(b, dict):
            continue
        beats.append({
            "title": str(b.get("title", "")).strip(),
            "goal": str(b.get("goal", "")).strip(),
            "duration_sec": int(b.get("duration_sec", 45) or 45),
            "visual_hint": str(b.get("visual_hint", "")).strip(),
            "content": str(b.get("content", "")).strip(),
            "text": "",
        })
    if not beats:
        raise RuntimeError("LLM не вернул биты — попробуй ещё раз")
    script["hooks"] = hooks
    script["hook_selected"] = 0
    script["beats"] = beats
    write_script(pdir, script)
    return script


@router.post("/{project_id:str}/script/structure")
async def generate_structure(project_id: str):
    project = _resolve_project(project_id)
    try:
        def run():
            job = jobs.project_job(project.dir.name, "Структура сценария", resource="llm")
            with job as cancel_event:
                current_project = _resolve_project(project_id)
                script = read_script(current_project.dir)
                with runlog.run(
                    current_project.dir,
                    "Структура сценария",
                    run_id=job.job_id,
                    job_id=job.job_id,
                    fields={
                        "operation": "script.structure",
                        "target_minutes": int(script.get("target_minutes", 8) or 8),
                    },
                ) as report:
                    jobs.check(cancel_event)
                    result = _structure_sync(current_project.dir, script)
                    jobs.check(cancel_event)
                    current_project.mark_stage_completed("script", result)
                    report.outcome(
                        "Структура сценария создана",
                        hooks=len(result.get("hooks", [])),
                        beats=len(result.get("beats", [])),
                    )
                    return result
        return await asyncio.to_thread(run)
    except (jobs.ProjectBusy, jobs.JobCancelled) as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Структура: {e}")


class BeatTextRequest(BaseModel):
    beat_index: int


def _beat_text_sync(pdir: Path, script: dict[str, Any], idx: int) -> dict[str, Any]:
    settings = load_settings()
    beats = script.get("beats", [])
    if idx < 0 or idx >= len(beats):
        raise ValueError("Бит не найден")
    beat = beats[idx]
    hooks = script.get("hooks", [])
    hook = hooks[min(script.get("hook_selected", 0), len(hooks) - 1)] if hooks else ""
    prev_text = beats[idx - 1].get("text", "") if idx > 0 else hook
    outline = "\n".join(
        f"  {i + 1}. {b.get('title')} — {b.get('goal')}" for i, b in enumerate(beats)
    )
    words = int(beat.get("duration_sec", 45)) * 2.3  # ~140 слов/мин начитки
    prompt = (
        f"Ты пишешь закадровый текст документального YouTube-ролика.\n"
        f"Тема: {script.get('topic')}\n"
        f"Структура ролика:\n{outline}\n\n"
        f"Сейчас пишем бит {idx + 1}: \"{beat.get('title')}\"\n"
        f"Цель бита: {beat.get('goal')}\n"
        f"План содержания: {beat.get('content')}\n"
        + (f"Конец предыдущего текста (продолжи связно, не повторяя):\n...{prev_text[-300:]}\n" if prev_text else "")
        + (f"Фактура (используй только эти факты):\n{script.get('facts')}\n" if script.get("facts") else "")
        + f"\nНапиши текст начитки этого бита: примерно {int(words)} слов "
        f"({beat.get('duration_sec')} секунд речи).\n"
        "Правила: разговорный, но точный язык; короткие предложения; конкретные цифры и образы вместо общих слов; "
        "без канцелярита и без 'давайте разберёмся'; активный залог; "
        "последнее предложение цепляет к следующему биту. Не выдумывай факты.\n"
        "Верни JSON: {\"text\": \"...\"}."
    )
    data = script_generation.lm_json(prompt, "Отвечай только валидным JSON. Не добавляй markdown.", settings)
    text = str(data.get("text", "")).strip()
    if not text:
        raise RuntimeError("Пустой текст")
    beat["text"] = text
    write_script(pdir, script)
    return {"ok": True, "beat_index": idx, "text": text, "script": script}


@router.post("/{project_id:str}/script/beat-text")
async def generate_beat_text(project_id: str, req: BeatTextRequest):
    project = _resolve_project(project_id)
    try:
        def run():
            job_name = f"Текст бита {req.beat_index + 1}"
            job = jobs.project_job(project.dir.name, job_name, resource="llm")
            with job as cancel_event:
                current_project = _resolve_project(project_id)
                script = read_script(current_project.dir)
                with runlog.run(
                    current_project.dir,
                    job_name,
                    run_id=job.job_id,
                    job_id=job.job_id,
                    fields={
                        "operation": "script.beat_text",
                        "beat_index": req.beat_index,
                    },
                ) as report:
                    jobs.check(cancel_event)
                    result = _beat_text_sync(current_project.dir, script, req.beat_index)
                    jobs.check(cancel_event)
                    current_project.mark_stage_completed("script", result["script"])
                    report.outcome(
                        "Текст бита создан",
                        words=len(result["text"].split()),
                    )
                    return result
        return await asyncio.to_thread(run)
    except (jobs.ProjectBusy, jobs.JobCancelled) as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Текст бита: {e}")


@router.post("/{project_id:str}/script/export")
async def export_script(project_id: str):
    """Concatenate hook + beat texts into 00_script.txt for recording/TTS."""
    project = _resolve_project(project_id)
    try:
        with jobs.project_mutation(project.dir.name, "Экспорт сценария"):
            current_project = _resolve_project(project_id)
            script = read_script(current_project.dir)
            final_text = str(script.get("final_text") or "").strip()
            hooks = script.get("hooks", [])
            hook = hooks[min(script.get("hook_selected", 0), len(hooks) - 1)] if hooks else ""
            parts = [final_text] if final_text else ([hook] if hook else [])
            missing = []
            if not final_text:
                for i, beat in enumerate(script.get("beats", [])):
                    if (beat.get("text") or "").strip():
                        parts.append(beat["text"].strip())
                    else:
                        missing.append(i + 1)
            if not parts:
                raise HTTPException(400, "Нет текста для экспорта — сгенерируй структуру и тексты битов")
            full = "\n\n".join(parts)
            output_path = current_project.dir / SCRIPT_TEXT_FILE
            atomic_write_text(output_path, full)
            current_project.mark_stage_completed(
                "script",
                {"script": script, "text": full},
                metadata={"exported_text": True},
            )
            return {
                "ok": True,
                "text": full,
                "path": str(output_path),
                "words": len(full.split()),
                "est_minutes": round(estimate_duration_sec(full, script.get("estimated_wpm", DEFAULT_WPM)) / 60, 1),
                "missing_beats": missing,
            }
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
