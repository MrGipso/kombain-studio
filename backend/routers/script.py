"""Script stage (step 00): topic -> structure (hooks + beats) -> narration text.

Professional pipelines design retention BEFORE recording: the hook and the beat
structure are authored in the script, not reverse-engineered from a finished
voice-over. This router stores the script in <project>/00_script.json, drives
the LLM for structure/text, and exports 00_script.txt for the user to record.
Scene analysis later prefers authored beats over embedding segmentation (see
core/script_align.py).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.config import load_settings
from core.project import open_project, safe_project_path, atomic_write_text
from core import lm

from backend.services import jobs, runlog

router = APIRouter()

SCRIPT_FILE = "00_script.json"
SCRIPT_TEXT_FILE = "00_script.txt"


def _resolve_project(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _default_script() -> dict[str, Any]:
    return {
        "topic": "",
        "target_minutes": 8,
        "audience_notes": "",
        "facts": "",
        "hooks": [],
        "hook_selected": 0,
        "beats": [],
    }


def _read_script(pdir: Path) -> dict[str, Any]:
    p = pdir / SCRIPT_FILE
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {**_default_script(), **data}
        except (json.JSONDecodeError, OSError):
            pass
    return _default_script()


def _write_script(pdir: Path, data: dict[str, Any]) -> None:
    atomic_write_text(pdir / SCRIPT_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def _lm_json(prompt: str, system: str, settings, timeout: int = 300) -> Any:
    """One LM Studio chat call that must return JSON (object or array).

    Thin wrapper kept for call-site stability; the actual client lives in
    core.lm (first strangler slice out of the monolith)."""
    try:
        return lm.chat_json(settings, prompt, system=system, timeout=timeout)
    except ValueError as exc:
        raise RuntimeError(str(exc))


@router.get("/{project_id:str}/script")
async def get_script(project_id: str):
    project = _resolve_project(project_id)
    return _read_script(project.dir)


@router.put("/{project_id:str}/script")
async def save_script(project_id: str, data: dict[str, Any]):
    project = _resolve_project(project_id)
    try:
        with jobs.project_mutation(project.dir.name, "Редактирование сценария"):
            # Re-open and re-read after acquiring the shared lock. A separate
            # running_job() check has a TOCTOU gap and can also merge into a
            # stale script snapshot.
            current_project = _resolve_project(project_id)
            current = _read_script(current_project.dir)
            current.update({k: v for k, v in data.items() if k in _default_script()})
            _write_script(current_project.dir, current)
            current_project.mark_stage_completed("script", current, metadata={"edited": True})
            return current
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
    data = _lm_json(prompt, "Отвечай только валидным JSON. Не добавляй markdown.", settings)
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
    _write_script(pdir, script)
    return script


@router.post("/{project_id:str}/script/structure")
async def generate_structure(project_id: str):
    project = _resolve_project(project_id)
    try:
        def run():
            job = jobs.project_job(project.dir.name, "Структура сценария", resource="llm")
            with job as cancel_event:
                current_project = _resolve_project(project_id)
                script = _read_script(current_project.dir)
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
    data = _lm_json(prompt, "Отвечай только валидным JSON. Не добавляй markdown.", settings)
    text = str(data.get("text", "")).strip()
    if not text:
        raise RuntimeError("Пустой текст")
    beat["text"] = text
    _write_script(pdir, script)
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
                script = _read_script(current_project.dir)
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
            script = _read_script(current_project.dir)
            hooks = script.get("hooks", [])
            hook = hooks[min(script.get("hook_selected", 0), len(hooks) - 1)] if hooks else ""
            parts = [hook] if hook else []
            missing = []
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
                "est_minutes": round(len(full.split()) / 140, 1),
                "missing_beats": missing,
            }
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
