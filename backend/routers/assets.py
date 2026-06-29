from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from core.config import load_settings
from core.project import open_project

import content_pipeline_gui as cpg
from backend.models.schemas import AssetCard

router = APIRouter()


def _resolve_project(project_id: str) -> Path:
    path = Path(project_id)
    if not path.is_absolute():
        output_base = Path(load_settings().output_base or "projects")
        path = output_base / project_id
    if not path.exists():
        raise HTTPException(404, "Project not found")
    return path


def _generate_sync(pdir: Path, card_index: int, mode: str) -> dict[str, Any]:
    """Blocking generation logic, runs in thread pool."""
    project = open_project(pdir, load_settings())
    if not project:
        raise RuntimeError("Project not found")

    comfy_url = project.settings.comfy_url or "http://127.0.0.1:8188"
    cards = cpg.load_asset_board(pdir)
    if card_index < 0 or card_index >= len(cards):
        raise RuntimeError("Card not found")

    card = cards[card_index]
    card.status = "generating"
    cpg.write_asset_board(pdir, cards)

    if mode == "route":
        if card.route == "remotion":
            _render_remotion(pdir, card, cards)
        elif card.route == "graphic" and card.visual_type == "motion_text":
            _render_motion_text(pdir, card, cards)
        else:
            if card.image_model:
                img_path = cpg.generate_comfy_image(comfy_url, pdir, card)
                card.image_output = str(img_path)
            if card.route == "image+video" and card.video_model:
                vid_path = cpg.generate_comfy_ltx_video(comfy_url, pdir, card)
                card.video_output = str(vid_path)
    elif mode == "image":
        img_path = cpg.generate_comfy_image(comfy_url, pdir, card)
        card.image_output = str(img_path)
        card.status = "image_ready"
        card.approved = False
        cpg.write_asset_board(pdir, cards)
        return {"ok": True, "card": card.__dict__}
    elif mode == "video":
        vid_path = cpg.generate_comfy_ltx_video(comfy_url, pdir, card)
        vid_path = cpg.apply_number_overlay_to_video(vid_path, pdir, card, print)
        card.video_output = str(vid_path)
    else:
        raise ValueError(f"Unknown mode: {mode}")

    card.status = "generated"
    card.approved = False
    cpg.write_asset_board(pdir, cards)
    return {"ok": True, "card": card.__dict__}


@router.get("/{project_id:str}/asset-board")
async def get_asset_board(project_id: str):
    pdir = _resolve_project(project_id)
    cards = cpg.load_asset_board(pdir)
    return [c.__dict__ for c in cards]


@router.post("/{project_id:str}/asset-board/save")
async def save_asset_board(project_id: str, data: list[dict[str, Any]]):
    pdir = _resolve_project(project_id)
    cards = [cpg.AssetCard(**item) for item in data]
    cpg.write_asset_board(pdir, cards)
    return {"ok": True, "count": len(cards)}


@router.put("/{project_id:str}/asset-board/{card_index}")
async def update_card(project_id: str, card_index: int, data: dict[str, Any]):
    pdir = _resolve_project(project_id)
    cards = cpg.load_asset_board(pdir)

    if card_index < 0 or card_index >= len(cards):
        raise HTTPException(404, "Card not found")

    card = cards[card_index]
    for key, value in data.items():
        if hasattr(card, key):
            setattr(card, key, value)

    cpg.write_asset_board(pdir, cards)
    return card.__dict__


@router.get("/{project_id:str}/asset-board/{card_index}")
async def get_card(project_id: str, card_index: int):
    pdir = _resolve_project(project_id)
    cards = cpg.load_asset_board(pdir)
    if card_index < 0 or card_index >= len(cards):
        raise HTTPException(404, "Card not found")
    return cards[card_index].__dict__


@router.post("/{project_id:str}/generate/{card_index}")
async def generate_card(project_id: str, card_index: int, mode: str = "route"):
    pdir = _resolve_project(project_id)
    try:
        result = await asyncio.to_thread(_generate_sync, pdir, card_index, mode)
        return result
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        # Mark card as failed
        try:
            cards = cpg.load_asset_board(pdir)
            if card_index < len(cards):
                cards[card_index].status = "needs_review"
                cards[card_index].notes = (cards[card_index].notes + "\n" if cards[card_index].notes else "") + f"Generation error: {e}"
                cpg.write_asset_board(pdir, cards)
        except Exception:
            pass
        raise HTTPException(500, str(e))


def _render_remotion(pdir: Path, card: cpg.AssetCard, cards: list):
    shots_path = pdir / "04_shot_list.json"
    shots_data = json.loads(shots_path.read_text("utf-8"))
    shots = [cpg.Shot(**item) for item in shots_data]
    shot = next((s for s in shots if s.index == card.shot_index), None)
    if not shot:
        raise RuntimeError(f"Shot {card.shot_index} not found")
    renders_dir = pdir / "renders" / "remotion"
    job = cpg.remotion_job_for_shot(shot, renders_dir)
    if not job:
        raise RuntimeError("No Remotion job for this card")
    _exec_remotion(pdir, card, cards, job)


def _render_motion_text(pdir: Path, card: cpg.AssetCard, cards: list):
    renders_dir = pdir / "renders" / "remotion"
    job = cpg.remotion_motion_text_job_for_card(card, renders_dir)
    _exec_remotion(pdir, card, cards, job)


def _exec_remotion(pdir: Path, card: cpg.AssetCard, cards: list, job: dict):
    jobs_dir = pdir / "remotion_jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    template = job["props"]["template"]
    job_path = jobs_dir / f"shot_{card.shot_index:03d}_{template}.json"
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), "utf-8")

    remotion_dir = Path(__file__).resolve().parent.parent.parent / "remotion_graphics"
    node = shutil.which("node.exe") or shutil.which("node")
    if not node:
        raise RuntimeError("node.exe not found in PATH")
    script = remotion_dir / "scripts" / "render-job.mjs"
    proc = subprocess.Popen(
        [node, str(script), str(job_path)], cwd=remotion_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout
    for line in proc.stdout:
        print(line.rstrip())
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"Remotion failed with code {code}")
    output = Path(job.get("output", ""))
    if not output.exists():
        raise RuntimeError(f"Expected output not found: {output}")
    card.remotion_output = str(output)
    card.status = "generated"
    cpg.write_asset_board(pdir, cards)
