from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.config import load_settings
from core import comfy_generation
from core import lm as lm_client
from core.project import atomic_write_text, open_project, safe_project_path
from core.export import find_music_track

from backend.services.sse_manager import SSEManager
from backend.services import jobs

router = APIRouter()
sse = SSEManager()


def _resolve_project(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _media_url(path: Path | str) -> str:
    return f"/api/media/{quote(str(path))}"


MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


def _tracks_meta_path(project) -> Path:
    return project.dir / "music" / "tracks.json"


def _read_tracks_meta(project) -> dict:
    p = _tracks_meta_path(project)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_tracks_meta(project, meta: dict) -> None:
    p = _tracks_meta_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(meta, ensure_ascii=False, indent=2))


def _list_tracks(project) -> list[dict]:
    music_dir = project.dir / "music"
    if not music_dir.exists():
        return []
    meta = _read_tracks_meta(project)
    files = [p for p in music_dir.iterdir() if p.is_file() and p.suffix.lower() in MUSIC_EXTS]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files:
        info = meta.get(p.name, {})
        out.append({
            "filename": p.name,
            "url": _media_url(p),
            "tags": info.get("tags", ""),
            "bpm": int(info.get("bpm", 0) or 0),
            "duration": int(info.get("duration", 0) or 0),
            "created": int(info.get("created") or p.stat().st_mtime),
            "trim_start": float(info.get("trim_start", 0) or 0),
            "trim_end": float(info.get("trim_end", 0) or 0),
        })
    return out


def _music_payload(project) -> dict:
    tracks = _list_tracks(project)
    valid = {t["filename"] for t in tracks}
    selected = str(project.metadata.get("music_selected", "")).strip()
    if selected not in valid:
        selected = tracks[0]["filename"] if tracks else ""
    sel_url = next((t["url"] for t in tracks if t["filename"] == selected), "")
    return {
        "music_tags": project.metadata.get("music_tags", ""),
        "music_bpm": int(project.metadata.get("music_bpm", 90) or 90),
        "music_duration": int(project.metadata.get("music_duration", 90) or 90),
        "music_enabled": bool(project.metadata.get("music_enabled", True)),
        "music_volume_db": float(project.metadata.get("music_volume_db", -18)),
        "music_duck": bool(project.metadata.get("music_duck", True)),
        "music_selected": selected,
        "tracks": tracks,
        "track_url": sel_url,
    }


@router.get("/{project_id:str}/music")
async def get_music(project_id: str):
    project = _resolve_project(project_id)
    return _music_payload(project)


class MusicSettingsRequest(BaseModel):
    music_tags: str = ""
    music_bpm: int = 90
    music_duration: int = 90
    music_enabled: bool = True
    music_volume_db: float = -18
    music_duck: bool = True


@router.put("/{project_id:str}/music/settings")
async def update_music_settings(project_id: str, req: MusicSettingsRequest):
    resolved = _resolve_project(project_id)
    try:
        with jobs.project_mutation(resolved.dir.name, "Изменение настроек музыки"):
            project = _resolve_project(project_id)
            project.metadata["music_tags"] = req.music_tags
            project.metadata["music_bpm"] = int(req.music_bpm)
            project.metadata["music_duration"] = int(req.music_duration)
            project.metadata["music_enabled"] = bool(req.music_enabled)
            project.metadata["music_volume_db"] = float(req.music_volume_db)
            project.metadata["music_duck"] = bool(req.music_duck)
            project.save_meta()
            project.invalidate_stage("timeline", reason="music settings changed")
            payload = _music_payload(project)
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
    return payload


def _lm_unload_delay_seconds(settings) -> int | None:
    auto_unload = getattr(settings, "lm_auto_unload", True)
    if isinstance(auto_unload, str):
        auto_unload = auto_unload.strip().lower() not in {"0", "false", "no", "off"}
    if not auto_unload:
        return None
    try:
        return max(0, int(str(getattr(settings, "lm_unload_delay", "0")).strip() or "0"))
    except ValueError:
        return 60


def _unload_lm_after_brief(settings) -> None:
    """Free the LLM from VRAM after the brief, same policy as scene analysis, so
    the GPU is clear before ACE-Step music generation runs in ComfyUI."""
    delay = _lm_unload_delay_seconds(settings)
    if delay is None or delay > 0:
        # Auto-unload disabled, or a TTL keeps the model warm — nothing to do.
        return
    model = str(getattr(settings, "lm_model", "") or "").strip()
    if not model or model == "local-model":
        return
    try:
        lm_client.unload_model(settings.lm_url or lm_client.DEFAULT_URL, model)
    except Exception:
        pass


def _lm_music_brief(base_url: str, model: str, notes: list[str], topic: str, ttl_seconds: int | None = None) -> dict:
    prompt = (
        "You are a music supervisor for a documentary-style narrated video.\n"
        "From the scene mood notes and the topic, design ONE cohesive background "
        "track that supports narration without overpowering it.\n"
        "Return JSON only: {\"tags\": \"...\", \"bpm\": <int>}.\n"
        "tags = a comma-separated ACE-Step style string: genre, mood, instruments, "
        "texture (e.g. 'cinematic ambient documentary, warm strings, soft piano, "
        "subtle low pulse, calm, unobtrusive, instrumental, no vocals').\n"
        "Do NOT put tempo, BPM or musical key inside tags — those are separate fields. "
        "Tags describe only style, mood, instruments and texture.\n"
        "No lyrics, no vocals. bpm is a single integer (usually 70-110 for docs).\n\n"
        f"Topic: {topic}\n"
        f"Scene mood notes:\n- " + "\n- ".join(n for n in notes if n) + "\n"
    )
    payload = {
        "model": (model or "local-model").strip(),
        "messages": [
            {"role": "system", "content": "Return only valid JSON. No markdown."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
    }
    lm_client.add_ttl(payload, ttl_seconds)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"].strip()
    start, end = content.find("{"), content.rfind("}")
    obj = json.loads(content[start:end + 1]) if start >= 0 and end > start else {}
    tags = str(obj.get("tags", "")).strip()
    try:
        bpm = int(obj.get("bpm", 90))
    except (TypeError, ValueError):
        bpm = 90
    return {"tags": tags, "bpm": max(40, min(bpm, 200))}


@router.post("/{project_id:str}/music/brief")
async def generate_music_brief(project_id: str):
    resolved = _resolve_project(project_id)
    settings = load_settings()
    delay = _lm_unload_delay_seconds(settings)
    ttl = delay if delay and delay > 0 else None

    def run_brief():
        with jobs.project_job(resolved.dir.name, "Музыкальный бриф", resource="llm") as cancel_event:
            try:
                project = _resolve_project(project_id)
                scenes = project.read_json("scenes") or []
                notes = [str(s.get("music_note", "")).strip() for s in scenes]
                jobs.check(cancel_event)
                brief = _lm_music_brief(
                    settings.lm_url or "http://127.0.0.1:1234/v1",
                    settings.lm_model or "local-model", notes, project.name, ttl,
                )
                jobs.check(cancel_event)
                # Metadata and DAG invalidation are part of this long job's
                # commit.  Releasing project_job before these writes allowed a
                # short UI mutation to interleave and be overwritten.
                if brief["tags"]:
                    project.metadata["music_tags"] = brief["tags"]
                project.metadata["music_bpm"] = brief["bpm"]
                project.save_meta()
                project.invalidate_stage("timeline", reason="music brief changed")
                return _music_payload(project)
            finally:
                _unload_lm_after_brief(settings)
    try:
        payload = await asyncio.to_thread(run_brief)
    except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Не удалось собрать бриф музыки: {exc}")
    return payload


@router.post("/{project_id:str}/music/generate")
async def generate_music(project_id: str):
    resolved = _resolve_project(project_id)
    project_key = resolved.dir.name

    async def event_stream():
        sub_id, q = sse.subscribe(project_key)
        loop = asyncio.get_event_loop()

        def log(msg: str):
            loop.call_soon_threadsafe(sse.emit_to, sub_id, "log", {"message": msg})

        def run_sync():
            try:
                with jobs.project_job(project_key, "Генерация музыки", resource="comfy") as cancel_event:
                    project = _resolve_project(project_id)
                    tags = str(project.metadata.get("music_tags", "")).strip()
                    bpm = int(project.metadata.get("music_bpm", 90) or 90)
                    duration = int(project.metadata.get("music_duration", 90) or 90)
                    comfy_url = project.settings.comfy_url or "http://127.0.0.1:8188"
                    log(f"Генерация музыки (ACE-Step): {duration}s, {bpm} bpm...")
                    jobs.check(cancel_event)

                    def check_cancelled() -> bool:
                        jobs.check(cancel_event)
                        return False

                    track = comfy_generation.generate_comfy_music(
                        comfy_url,
                        project.dir,
                        tags,
                        bpm=bpm,
                        seconds=duration,
                        cancel_check=check_cancelled,
                        settings=project.settings,
                    )
                    jobs.check(cancel_event)
                    # Record the track in the library and make it the active one.
                    tmeta = _read_tracks_meta(project)
                    tmeta[track.name] = {
                        "tags": tags, "bpm": bpm, "duration": duration, "created": int(time.time()),
                    }
                    _write_tracks_meta(project, tmeta)
                    project.metadata["music_selected"] = track.name
                    project.save_meta()
                    project.invalidate_stage("timeline", reason="music track generated")
                loop.call_soon_threadsafe(
                    sse.emit_to, sub_id, "done",
                    {"track_path": str(track), "track_url": _media_url(track), "filename": track.name},
                )
            except jobs.JobCancelled as exc:
                loop.call_soon_threadsafe(
                    sse.emit_to,
                    sub_id,
                    "error",
                    {"message": str(exc), "code": "cancelled"},
                )
            except Exception as exc:
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "error", {"message": str(exc)})

        loop.run_in_executor(None, run_sync)
        async for msg in sse.generator(sub_id):
            yield msg

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class SelectTrackRequest(BaseModel):
    filename: str


@router.put("/{project_id:str}/music/select")
async def select_track(project_id: str, req: SelectTrackRequest):
    resolved = _resolve_project(project_id)
    try:
        with jobs.project_mutation(resolved.dir.name, "Выбор музыкального трека"):
            project = _resolve_project(project_id)
            name = Path(req.filename).name  # guard against path traversal
            if not (project.dir / "music" / name).exists():
                raise HTTPException(404, "Track not found")
            project.metadata["music_selected"] = name
            project.save_meta()
            project.invalidate_stage("timeline", reason="selected music track changed")
            payload = _music_payload(project)
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
    return payload


class TrimRequest(BaseModel):
    filename: str
    trim_start: float = 0
    trim_end: float = 0


@router.put("/{project_id:str}/music/trim")
async def set_trim(project_id: str, req: TrimRequest):
    resolved = _resolve_project(project_id)
    try:
        with jobs.project_mutation(resolved.dir.name, "Обрезка музыкального трека"):
            project = _resolve_project(project_id)
            name = Path(req.filename).name
            if not (project.dir / "music" / name).exists():
                raise HTTPException(404, "Track not found")
            tmeta = _read_tracks_meta(project)
            info = dict(tmeta.get(name, {}))
            info["trim_start"] = max(0.0, float(req.trim_start))
            info["trim_end"] = max(0.0, float(req.trim_end))
            tmeta[name] = info
            _write_tracks_meta(project, tmeta)
            project.invalidate_stage("timeline", reason="music trim changed")
            payload = _music_payload(project)
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
    return payload


@router.delete("/{project_id:str}/music/track/{filename}")
async def delete_track(project_id: str, filename: str):
    resolved = _resolve_project(project_id)
    try:
        with jobs.project_mutation(resolved.dir.name, "Удаление музыкального трека"):
            project = _resolve_project(project_id)
            name = Path(filename).name  # guard against path traversal
            target = project.dir / "music" / name
            if target.exists():
                target.unlink()
            tmeta = _read_tracks_meta(project)
            if name in tmeta:
                tmeta.pop(name, None)
                _write_tracks_meta(project, tmeta)
            if str(project.metadata.get("music_selected", "")).strip() == name:
                project.metadata["music_selected"] = ""
                project.save_meta()
            project.invalidate_stage("timeline", reason="music track deleted")
            payload = _music_payload(project)
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
    return payload
