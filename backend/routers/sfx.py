"""SFX page API: per-shot ambience queries, Woosh generation, custom uploads.

The asset board is the single source of truth — this router only reads/writes
`sfx_query` / `sfx_output` on cards, so the export mixes exactly what the page
shows. Consecutive cards with the same query share one sound file (the same
grouping the export bed uses).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from core.config import load_settings
from core.project import safe_project_path
from core import sfx as sfx_lib
from backend.services import jobs, runlog

from core import board as board_store

router = APIRouter()

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def _resolve_project(project_id: str) -> Path:
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    if not path.exists():
        raise HTTPException(404, "Project not found")
    return path


def _media_url(path: str | Path) -> str:
    return f"/api/media/{quote(str(path))}" if path else ""


def _items(cards: list) -> list[dict[str, Any]]:
    out = []
    for i, c in enumerate(cards):
        sfx_output = (getattr(c, "sfx_output", "") or "").strip()
        has_file = bool(sfx_output) and Path(sfx_output).exists()
        out.append({
            "card_index": i,
            "shot_index": c.shot_index,
            "scene_title": c.scene_title,
            "start": c.start,
            "end": c.end,
            "description": c.description,
            "status": c.status,
            "sfx_query": getattr(c, "sfx_query", "") or "",
            "sfx_output": sfx_output if has_file else "",
            "url": _media_url(sfx_output) if has_file else "",
        })
    return out


@router.get("/{project_id:str}/sfx")
async def get_sfx(project_id: str):
    pdir = _resolve_project(project_id)
    settings = load_settings()
    cards = board_store.load_asset_board(pdir)
    enabled = bool(getattr(settings, "sfx_enabled", False))
    woosh_url = (getattr(settings, "woosh_url", "") or "").strip()
    online = False
    if woosh_url:
        online = await asyncio.to_thread(sfx_lib.woosh_ping, woosh_url, 3)
    return {
        "sfx_enabled": enabled,
        "woosh_url": woosh_url,
        "woosh_online": online,
        "sfx_volume_db": float(getattr(settings, "sfx_volume_db", -24.0)),
        "items": _items(cards),
    }


class QueryRequest(BaseModel):
    card_indices: list[int]
    sfx_query: str = ""


@router.put("/{project_id:str}/sfx/query")
async def set_query(project_id: str, req: QueryRequest):
    pdir = _resolve_project(project_id)
    try:
        with jobs.project_mutation(pdir.name, "Изменение SFX-запроса"):
            # The board is an RMW document.  Load it only after the shared
            # project lock is held, otherwise a render can commit between the
            # read and this write and be silently overwritten.
            cards = board_store.load_asset_board(pdir)
            for idx in req.card_indices:
                if idx < 0 or idx >= len(cards):
                    raise HTTPException(404, f"Card {idx} not found")

            query = req.sfx_query.strip()
            changed = False
            for idx in req.card_indices:
                card = cards[idx]
                if (getattr(card, "sfx_query", "") or "").strip() != query:
                    card.sfx_query = query
                    card.sfx_output = ""  # query changed -> old sound no longer matches
                    changed = True
            if changed:
                board_store.write_asset_board(pdir, cards)
            items = _items(cards)
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "items": items}


class GenerateRequest(BaseModel):
    card_indices: list[int]
    fresh: bool = False  # True = new random take ("перегенерировать")


@router.post("/{project_id:str}/sfx/generate")
async def generate(project_id: str, req: GenerateRequest):
    pdir = _resolve_project(project_id)
    settings = load_settings()
    if not req.card_indices:
        raise HTTPException(400, "card_indices is empty")

    woosh_url = (getattr(settings, "woosh_url", "") or "").strip()

    try:
        def run():
            job = jobs.project_job(pdir.name, "Генерация SFX", resource="woosh")
            with job as cancel_event:
                cards = board_store.load_asset_board(pdir)
                for idx in req.card_indices:
                    if idx < 0 or idx >= len(cards):
                        raise HTTPException(404, f"Card {idx} not found")
                query = (
                    getattr(cards[req.card_indices[0]], "sfx_query", "") or ""
                ).strip()
                if not query:
                    raise HTTPException(400, "У кадра нет sfx-запроса")
                with runlog.run(
                    pdir,
                    "Генерация SFX",
                    run_id=job.job_id,
                    job_id=job.job_id,
                    fields={
                        "operation": "sfx.generate",
                        "card_indices": req.card_indices,
                        "fresh": req.fresh,
                        "query": query,
                    },
                ) as report:
                    jobs.check(cancel_event)
                    if not woosh_url or not sfx_lib.woosh_ping(woosh_url, 3):
                        raise RuntimeError("Woosh недоступен — запустите start_woosh.bat")
                    result = sfx_lib.generate_for_query(pdir, query, settings, req.fresh)
                    jobs.check(cancel_event)
                    for idx in req.card_indices:
                        cards[idx].sfx_output = str(result)
                    board_store.write_asset_board(pdir, cards)
                    report.outcome(
                        "SFX создан",
                        output=str(result),
                        cards_updated=len(req.card_indices),
                    )
                    return result, cards
        dst, cards = await asyncio.to_thread(run)
    except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
        raise HTTPException(409, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Генерация не удалась: {exc}")

    return {"ok": True, "sfx_output": str(dst), "url": _media_url(dst), "items": _items(cards)}


@router.post("/{project_id:str}/sfx/upload")
async def upload(project_id: str, file: UploadFile = File(...), card_indices: str = Form(...)):
    pdir = _resolve_project(project_id)
    try:
        indices = [int(x) for x in card_indices.split(",") if x.strip() != ""]
    except ValueError:
        raise HTTPException(400, "Bad card_indices")
    if not indices:
        raise HTTPException(400, "card_indices is empty")

    name = Path(file.filename or "sound").name
    ext = Path(name).suffix.lower()
    if ext not in AUDIO_EXTS:
        raise HTTPException(400, f"Неподдерживаемый формат: {ext or '(нет расширения)'}")

    # Receiving a potentially large request must not hold the project lock.
    # Stage it on the same volume so the later replace is atomic, then commit
    # the file and the board RMW while the shared project lock is held.
    staged = pdir / f".sfx-upload-{uuid.uuid4().hex}.tmp"
    dst: Path | None = None
    board_written = False
    try:
        written = 0
        with staged.open("xb") as stream:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Файл больше 100 МБ")
                stream.write(chunk)
        if written == 0:
            raise HTTPException(400, "Пустой файл")

        try:
            with jobs.project_mutation(pdir.name, "Загрузка SFX"):
                cards = board_store.load_asset_board(pdir)
                for idx in indices:
                    if idx < 0 or idx >= len(cards):
                        raise HTTPException(404, f"Card {idx} not found")

                sfx_dir = pdir / "sfx"
                sfx_dir.mkdir(parents=True, exist_ok=True)
                dst = sfx_dir / f"custom_{time.time_ns()}_{name}"
                staged.replace(dst)
                for idx in indices:
                    cards[idx].sfx_output = str(dst)
                try:
                    board_store.write_asset_board(pdir, cards)
                except BaseException:
                    dst.unlink(missing_ok=True)
                    raise
                board_written = True
                items = _items(cards)
        except jobs.ProjectBusy as exc:
            raise HTTPException(409, str(exc))
    finally:
        staged.unlink(missing_ok=True)
        if dst is not None and not board_written:
            dst.unlink(missing_ok=True)

    return {
        "ok": True,
        "sfx_output": str(dst),
        "url": _media_url(dst),
        "items": items,
    }
