from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.config import load_settings
from core.project import open_project

import content_pipeline_gui as cpg
from backend.models.schemas import AssetCard

router = APIRouter()


class RegeneratePromptRequest(BaseModel):
    target: str = "image_prompt"




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

    if mode in {"route", "graphic"}:
        card.route = "image+video"
        card.visual_type = "ai_image_then_i2v"
        if not card.image_model:
            card.image_model = cpg.preferred_realistic_image_model(project.settings.image_model_preference)
        if not card.video_model:
            card.video_model = "ltx2.3_i2v"
        if card.image_model:
            img_path = cpg.generate_comfy_image(comfy_url, pdir, card)
            card.image_output = str(img_path)
        if card.video_model:
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
        card.video_output = str(vid_path)
    else:
        raise ValueError(f"Unknown mode: {mode}")

    card.status = "generated"
    card.approved = False
    cpg.write_asset_board(pdir, cards)
    return {"ok": True, "card": card.__dict__}



def _extract_json_object(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    data_text = cleaned
    starts = [(pos, opener) for pos, opener in ((cleaned.find("{"), "{"), (cleaned.find("["), "[")) if pos >= 0]
    if starts:
        start, opener = min(starts, key=lambda item: item[0])
        closer = "}" if opener == "{" else "]"
        end = cleaned.rfind(closer)
        if end > start:
            data_text = cleaned[start:end + 1]

    data = json.loads(data_text)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                return item
        raise ValueError("LM Studio returned JSON array without an object")
    if not isinstance(data, dict):
        raise ValueError("LM Studio returned non-object JSON")
    return data


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


def _lm_prompt(endpoint: str, model: str, prompt: str, ttl_seconds: int | None = None) -> dict[str, Any]:
    payload = {
        "model": model.strip() or "local-model",
        "messages": [
            {"role": "system", "content": "Return only valid JSON. No markdown."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
    }
    cpg.add_lm_ttl(payload, ttl_seconds)
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return _extract_json_object(content)


def _prompt_from_lm_data(data: dict[str, Any], target: str) -> str:
    keys = ["prompt", target]
    if target == "image_prompt":
        keys.extend(["image", "imagePrompt"])
    else:
        keys.extend(["video", "videoPrompt"])
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _unload_lm_after_prompt(settings) -> dict[str, Any]:
    delay = _lm_unload_delay_seconds(settings)
    if delay is None:
        return {"ok": True, "skipped": True, "message": "LM Studio auto-unload disabled"}
    if delay > 0:
        return {"ok": True, "skipped": True, "delay": delay, "message": f"LM Studio TTL set ({delay}s)"}

    model = str(settings.lm_model or "").strip()
    if not model or model == "local-model":
        return {"ok": False, "skipped": True, "message": "LM Studio model is not configured"}
    try:
        result = cpg.unload_lm_studio_model(settings.lm_url or "http://127.0.0.1:1234/v1", model)
        return {"ok": True, "skipped": False, "result": result}
    except Exception as exc:
        return {"ok": False, "skipped": False, "message": cpg.explain_lm_studio_error(exc)}

def _regenerate_prompt_sync(pdir: Path, card_index: int, target: str) -> dict[str, Any]:
    project = open_project(pdir, load_settings())
    if not project:
        raise RuntimeError("Project not found")

    target = target if target in {"image_prompt", "video_prompt"} else "image_prompt"
    cards = cpg.load_asset_board(pdir)
    if card_index < 0 or card_index >= len(cards):
        raise RuntimeError("Card not found")

    card = cards[card_index]
    settings = load_settings()
    video_style = project.metadata.get("video_style", "knitted")
    style_anchor = project.metadata.get("style_anchor", "")
    character_description = project.metadata.get("character_description", "")
    base_context = (
        f"Video style: {video_style}\n"
        f"Scene title: {card.scene_title}\n"
        f"Short description: {card.description}\n"
        f"Screen text: {card.screen_text}\n"
        f"Visual type: {card.visual_type}\n"
        f"Route: {card.route}\n"
        f"Asset query: {card.asset_query}\n"
        f"Style anchor: {style_anchor}\n"
        f"Character description: {character_description}\n"
    )

    if target == "image_prompt":
        prompt = (
            "Create one strong English image-generation prompt for Krea 2 in the selected video style.\n"
            "Use the short description as the source of truth, then enrich it with concrete subject, location, materials, lighting, camera angle, composition, colors, and atmosphere.\n"
            "Translate abstract ideas into simple visible props/actions that fit the selected style.\n"
            "Do not add readable text, logos, UI labels, LoRA names, or trigger phrases.\n"
            "Start with the concrete visible subject, not with 'documentary photo' or 'cinematic'.\n"
            "Return JSON: {\"prompt\": \"...\"}.\n\n"
            f"Context:\n{base_context}\n"
            f"Current image prompt, if any:\n{card.image_prompt}\n"
        )
    else:
        prompt = (
            "Create one strong English video-generation prompt for an image-to-video model.\n"
            "Focus on what changes over time: camera movement, subject motion, environmental motion, pacing, and style-safe subtle animation.\n"
            "Do not repeat the full visual appearance from the image prompt; assume the image already defines the look.\n"
            "Use present tense. Avoid text/logos and avoid asking for subtitles.\n"
            "Return JSON: {\"prompt\": \"...\"}.\n\n"
            f"Context:\n{base_context}\n"
            f"Image prompt:\n{card.image_prompt}\n"
            f"Current video prompt, if any:\n{card.video_prompt}\n"
        )

    delay = _lm_unload_delay_seconds(settings)
    ttl = delay if delay and delay > 0 else None
    data = _lm_prompt(
        settings.lm_url or "http://127.0.0.1:1234/v1",
        settings.lm_model or "local-model",
        prompt,
        ttl_seconds=ttl,
    )
    new_prompt = _prompt_from_lm_data(data, target)
    if not new_prompt:
        raise RuntimeError("LM Studio returned an empty prompt")

    if target == "image_prompt":
        new_prompt = cpg.compose_image_prompt(
            video_style,
            new_prompt,
            character_description=character_description,
            custom_notes=style_anchor,
        )
    else:
        new_prompt = cpg.compose_video_prompt(video_style, new_prompt, shot_description=card.description)

    setattr(card, target, new_prompt)
    cpg.write_asset_board(pdir, cards)
    unload = _unload_lm_after_prompt(settings)
    return {"ok": True, "target": target, "prompt": new_prompt, "card": card.__dict__, "lm_unload": unload}

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



@router.post("/{project_id:str}/asset-board/{card_index}/regenerate-prompt")
async def regenerate_card_prompt(project_id: str, card_index: int, req: RegeneratePromptRequest):
    pdir = _resolve_project(project_id)
    try:
        return await asyncio.to_thread(_regenerate_prompt_sync, pdir, card_index, req.target)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))

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


# Remotion rendering and the /render composition endpoint were removed:
# the pipeline is now pure AI video (ComfyUI image -> LTX i2v), assembled by
# the ffmpeg export on the Preview page.
