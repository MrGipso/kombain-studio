from __future__ import annotations

import asyncio
import hashlib
import json
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError

from core.config import load_settings
from core.project import content_hash, is_within, open_project, safe_project_path
from core import board as asset_board
from core import stock as stock_lib
from core import kenburns
from core import reference as reference_lib
from core import asset_variants
from core import asset_qa
from core import asset_qa_store
from core import canonical_references
from core import comfy as comfy_client
from core import comfy_generation
from core import lm as lm_client
from core import production
from core.models import AssetCard as CoreAssetCard, timecode_to_seconds
from core.style_presets import compose_image_prompt, compose_video_prompt
from backend.services import jobs, material_engine

from backend.models.schemas import (
    AssetCard as AssetCardSchema,
    AssetVariantList,
    AssetVariantSelectResult,
)

router = APIRouter()


def _load_asset_board(project_dir: Path) -> list[CoreAssetCard]:
    return asset_board.load_asset_board(project_dir)


def _write_asset_board(project_dir: Path, cards: list[CoreAssetCard]) -> None:
    asset_board.write_asset_board(project_dir, cards)


def _build_asset_board(project_dir: Path) -> list[CoreAssetCard]:
    return asset_board.build_asset_board(project_dir)


def _sync_asset_card_route(
    card: CoreAssetCard,
    image_preference: str = "Auto",
) -> CoreAssetCard:
    return asset_board.sync_asset_card_route(card, image_preference)


# These fields are interpreted as local files by the exporter and/or copied to
# the immutable variant store.  Treating them as ordinary user-editable strings
# allowed a crafted board update to make the server read and snapshot an
# arbitrary absolute file.  Existing legacy values are left untouched so old
# projects remain editable, but every newly supplied value must stay in the
# project directory.
USER_MEDIA_PATH_FIELDS: tuple[str, ...] = (
    *asset_variants.OUTPUT_FIELDS,
    "sfx_output",
)


class RegeneratePromptRequest(BaseModel):
    target: Literal["image_prompt", "video_prompt"] = "image_prompt"


class MaterialRunRequest(BaseModel):
    mode: Literal["unattended"] = "unattended"
    max_image_attempts: int = 2
    max_video_attempts: int = 2
    fallback: Literal["kenburns"] = "kenburns"
    skip_ready: bool = True
    auto_resume: bool = True
    technical_validation: bool = True




def _resolve_project(project_id: str) -> Path:
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    if not path.exists():
        raise HTTPException(404, "Project not found")
    return path


def _record_outputs(
    pdir: Path,
    card_index: int,
    card,
    project,
    *,
    source: str,
    mode: str,
    output_fields: list[str],
    metadata: dict[str, Any] | None = None,
    force: bool = True,
) -> list[dict[str, Any]]:
    """Snapshot successful outputs before their conventional paths are reused."""
    return asset_variants.record_card_outputs(
        pdir,
        card_index=card_index,
        card=card,
        source=source,
        mode=mode,
        settings=project.settings,
        output_fields=output_fields,
        metadata=metadata,
        force=force,
    )


def _record_manual_card_change(pdir: Path, index: int, old, card, project, *, mode: str) -> None:
    changed = [
        field
        for field in asset_variants.OUTPUT_FIELDS
        if str(getattr(old, field, "") or "") != str(getattr(card, field, "") or "")
    ]
    if not changed:
        return
    for field in changed:
        if not str(getattr(card, field, "") or "").strip():
            asset_variants.deselect_output(
                pdir,
                index,
                field,
                reason=f"manual:{mode}",
                card_id=getattr(card, "card_id", ""),
            )
    _record_outputs(
        pdir,
        index,
        card,
        project,
        source="manual",
        mode=mode,
        output_fields=changed,
        force=False,
    )


def _record_manual_changes(pdir: Path, previous: list, current: list, *, mode: str) -> None:
    """Record media paths changed through the editable board API."""
    project = open_project(pdir, load_settings())
    if not project:
        raise RuntimeError("Project not found")
    # Upgrade legacy index-only history before the incoming board can reorder
    # rows.  Existing records remain immutable apart from gaining card_id.
    asset_variants.bind_card_identities(
        pdir,
        {
            index: str(getattr(card, "card_id", "") or "")
            for index, card in enumerate(previous)
        },
    )
    previous_by_id = {
        str(getattr(card, "card_id", "") or ""): card
        for card in previous
        if str(getattr(card, "card_id", "") or "")
    }
    for index, card in enumerate(current):
        current_id = str(getattr(card, "card_id", "") or "")
        old = previous_by_id.get(current_id)
        if old is None and not current_id and index < len(previous):
            old = previous[index]
        _record_manual_card_change(pdir, index, old, card, project, mode=mode)


def _card_value(card: Any | None, field: str) -> str:
    if card is None:
        return ""
    if isinstance(card, dict):
        value = card.get(field, "")
    else:
        value = getattr(card, field, "")
    return str(value or "").strip()


def _validate_user_media_paths(pdir: Path, previous: list, current: list) -> None:
    """Reject newly supplied media paths that escape ``pdir``.

    Unchanged external paths are tolerated for backwards compatibility.  They
    are not recorded again because ``_record_manual_changes`` also compares the
    old and new value.  Clearing a legacy value is always allowed.
    """
    violations: list[dict[str, Any]] = []
    previous_by_id = {
        str(getattr(card, "card_id", "") or ""): card
        for card in previous
        if str(getattr(card, "card_id", "") or "")
    }
    for index, card in enumerate(current):
        current_id = str(getattr(card, "card_id", "") or "")
        old = previous_by_id.get(current_id)
        if old is None and not current_id and index < len(previous):
            old = previous[index]
        for field in USER_MEDIA_PATH_FIELDS:
            value = _card_value(card, field)
            old_value = _card_value(old, field)
            if not value or value == old_value:
                continue
            try:
                candidate = Path(value).expanduser()
                if not candidate.is_absolute():
                    candidate = pdir / candidate
                allowed = is_within(candidate, pdir)
            except (OSError, RuntimeError, ValueError):
                allowed = False
            if not allowed:
                violations.append({"card_index": index, "field": field})

    if violations:
        raise HTTPException(
            403,
            {
                "message": "Media paths supplied by the Asset Board must stay inside the project directory.",
                "violations": violations,
            },
        )


def _cards_from_api(
    data: list[AssetCardSchema],
    previous: list[CoreAssetCard],
) -> list[CoreAssetCard]:
    """Preserve ids for older clients while accepting explicit-id reorders."""
    previous_ids = {
        str(card.card_id or "").strip() for card in previous if card.card_id
    }
    previous_by_shot: dict[int, list[Any]] = {}
    for card in previous:
        previous_by_shot.setdefault(int(card.shot_index), []).append(card)

    cards: list[CoreAssetCard] = []
    for index, item in enumerate(data):
        values = item.model_dump()
        supplied_id = "card_id" in item.model_fields_set and bool(
            str(values.get("card_id", "") or "").strip()
        )
        if supplied_id and str(values["card_id"]).strip() not in previous_ids:
            candidates = previous_by_shot.get(int(item.shot_index), [])
            if len(candidates) == 1:
                raise HTTPException(409, "AssetCard card_id is immutable")
        if not supplied_id:
            candidates = previous_by_shot.get(int(item.shot_index), [])
            old = candidates[0] if len(candidates) == 1 else (
                previous[index] if index < len(previous) else None
            )
            if old is not None:
                values["card_id"] = old.card_id
        cards.append(CoreAssetCard(**values))

    ids = [str(card.card_id or "").strip() for card in cards]
    duplicate_ids = sorted({card_id for card_id in ids if ids.count(card_id) > 1})
    if duplicate_ids:
        raise HTTPException(
            422,
            {"message": "Duplicate AssetCard card_id", "card_ids": duplicate_ids},
        )
    return cards


def _qa_generated_outputs(
    pdir: Path,
    card,
    settings,
    *,
    image: bool = False,
    video: bool = False,
    cancel_check: Callable[[], Any] | None = None,
    log_note: str = "QA",
) -> list[dict[str, Any]]:
    """Record hash-bound QA evidence without ever triggering generation.

    Shadow mode is observation-only. Review mode may only flag needs_review.
    A QA outage cannot turn a successful render into an HTTP 500, but unlike
    the legacy scalar scorer it is returned to the caller instead of vanishing.
    """
    # A legacy scalar cannot describe newly generated bytes. Keep the field for
    # old projects/API compatibility, but explicitly mark it as not current.
    card.qa_score = -1.0
    if not bool(getattr(settings, "ai_frame_qa", False)):
        return []

    reports: list[dict[str, Any]] = []
    operations = []
    if image and str(getattr(card, "image_output", "") or "").strip():
        operations.append(asset_qa.inspect_image)
    if video and str(getattr(card, "video_output", "") or "").strip():
        operations.append(asset_qa.inspect_video)
    for operation in operations:
        try:
            reports.append(
                operation(pdir, card, settings, cancel_check=cancel_check)
            )
        except Exception as exc:
            # If this is cooperative cancellation, the callback re-raises it.
            # Otherwise expose the QA failure while preserving render success.
            if cancel_check is not None:
                cancel_check()
            reports.append({
                "kind": "image" if operation is asset_qa.inspect_image else "video",
                "mode": str(getattr(settings, "ai_qa_mode", "shadow") or "shadow"),
                "recommendation": "needs_review",
                "summary": "QA-отчёт не удалось сохранить; проверьте журнал и файл вручную.",
                "error": str(exc),
                "applied_action": "none",
            })
    asset_qa.apply_review_mode(card, reports, log_note=log_note)
    return reports


def _generate_sync(
    pdir: Path,
    card_index: int,
    mode: str,
    skip_stock: bool = False,
    force_full: bool = False,
    *,
    cancel_check: Callable[[], bool] | None = None,
    defer_qa: bool = False,
    reference_image: Path | None = None,
) -> dict[str, Any]:
    """Blocking generation logic, runs in thread pool. When skip_stock is True,
    a stock-routed card is generated with AI directly (no stock lookup). When
    force_full is True, the LTX video is rendered with the full workflow even if
    draft mode is on — used by the "Финал" action to finalize a drafted clip."""
    project = open_project(pdir, load_settings())
    if not project:
        raise RuntimeError("Project not found")

    # Batch queues (ai_qa_timing=after_queue) run QA as one final phase via
    # POST /asset-board/{i}/qa, so the vision model is loaded once instead of
    # swapping VRAM with Krea/LTX after every card.
    run_qa = _qa_generated_outputs
    if defer_qa:
        def run_qa(_pdir, card, _settings, **_kwargs):
            card.qa_score = -1.0  # freshly generated bytes: old score not current
            return []

    comfy_url = project.settings.comfy_url or "http://127.0.0.1:8188"
    cards = _load_asset_board(pdir)
    if card_index < 0 or card_index >= len(cards):
        raise RuntimeError("Card not found")

    card = cards[card_index]
    _sync_asset_card_route(card, project.settings.image_model_preference)
    card.status = "generating"
    _write_asset_board(pdir, cards)

    # Reference-insert card (Wikipedia / arXiv): resolve the named entity to a
    # real page, screenshot it and slide it in over a blurred backdrop. On any
    # miss (no page / capture failed) fall back to a Krea still with Ken Burns.
    if (card.visual_type or "").lower() == "web_reference_card" and mode in {"route", "graphic"}:
        rlog: list[str] = []
        lang = str(project.metadata.get("detected_language", "") or "en")
        ref_res = reference_lib.fetch_reference_for_card(
            pdir, card, load_settings(), lang=lang, log=rlog.append,
            cancel_check=cancel_check,
        )
        if ref_res:
            card.video_output = ref_res["video_output"]
            card.image_output = ref_res.get("image_output", "") or card.image_output
            card.reference_url = ref_res.get("reference_url", "")
            card.reference_source = ref_res.get("reference_source", "") or card.reference_source
            card.reference_credit = ref_res.get("reference_credit", "")
            card.source_url = ref_res.get("source_url", "")
            card.route = "reference"
            card.status = "generated"
            card.approved = False
            qa = run_qa(
                pdir,
                card,
                project.settings,
                image=bool(card.image_output),
                video=True,
                cancel_check=cancel_check,
            )
            variants = _record_outputs(
                pdir,
                card_index,
                card,
                project,
                source="reference",
                mode=mode,
                output_fields=["image_output", "video_output"],
                metadata={
                    "reference_title": ref_res.get("reference_title", ""),
                    "resolver_source": ref_res.get("reference_source", ""),
                },
            )
            _write_asset_board(pdir, cards)
            return {
                "ok": True,
                "card": card.__dict__,
                "reference": True,
                "variants": variants,
                "qa": qa,
            }
        reason = rlog[-1] if rlog else "Источник не найден"
        # Keep the persisted route contract aligned with visual_type.  Without
        # this, the next attempt sees the stale `reference` route and converts
        # the card back to a reference insert before Ken Burns can run.
        card.route = "kenburns"
        card.visual_type = "still_kenburns"
        card.notes = (card.notes + "\n" if card.notes else "") + f"Источник не получен ({reason}) → Ken Burns"
        _write_asset_board(pdir, cards)

    # Stock-routed card: layered stock search (accept/floor + query retries). On a
    # real miss, fall back to a cheap Ken Burns fill (below) rather than a gap or
    # expensive AI. "Дополнить AI" (skip_stock) forces AI instead.
    if (not skip_stock) and (card.visual_type or "").lower() == "stock_video" and mode in {"route", "graphic"}:
        slog: list[str] = []
        stock_res = stock_lib.fetch_stock_for_card(
            pdir, card, load_settings(), log=slog.append, cancel_check=cancel_check
        )
        if stock_res:
            card.video_output = stock_res["video_output"]
            card.source_url = stock_res.get("source_url", "")
            card.source_license = stock_res.get("license", "")
            card.source_author = stock_res.get("author", "")
            card.stock_score = float(stock_res.get("stock_score", 0.0))
            card.route = "stock"
            card.status = "generated"
            card.approved = False
            qa = run_qa(
                pdir,
                card,
                project.settings,
                video=True,
                cancel_check=cancel_check,
            )
            variants = _record_outputs(
                pdir,
                card_index,
                card,
                project,
                source="stock",
                mode=mode,
                output_fields=["video_output"],
                metadata={"provider": stock_res.get("source", "")},
            )
            _write_asset_board(pdir, cards)
            return {
                "ok": True,
                "card": card.__dict__,
                "source": stock_res.get("source", ""),
                "stock": True,
                "variants": variants,
                "qa": qa,
            }
        # No stock after retries -> fall back to a cheap Ken Burns fill (gap-free)
        # by re-routing the card; the Ken Burns branch below then handles it.
        reason = slog[-1] if slog else "Подходящий сток не найден"
        card.visual_type = "still_kenburns"
        card.notes = (card.notes + "\n" if card.notes else "") + f"Сток не найден ({reason}) → Ken Burns"
        _write_asset_board(pdir, cards)

    # Ken Burns route: a Krea still with a slow zoom (cheap alternative to i2v).
    if (card.visual_type or "").lower() == "still_kenburns" and mode in {"route", "graphic"}:
        if not card.image_model:
            card.image_model = asset_board.preferred_realistic_image_model(project.settings.image_model_preference)
        generated_image = not (card.image_output and Path(card.image_output).exists())
        if generated_image:
            img_path = comfy_generation.generate_comfy_image(
                comfy_url,
                pdir,
                card,
                cancel_check=cancel_check,
                settings=project.settings,
                reference_image=reference_image,
            )
            card.image_output = str(img_path)
        seconds = max(2.0, timecode_to_seconds(card.end) - timecode_to_seconds(card.start))
        dst = pdir / "assets" / "videos" / f"shot_{card.shot_index:03d}_kenburns.mp4"
        # Vary the move per shot (zoom in/out, 4 pans, eased) so a run of stills
        # doesn't repeat one identical center zoom; documentary style also gets a
        # subtle handheld wobble. Deterministic by shot index.
        settings = load_settings()
        variety = bool(getattr(settings, "kenburns_variety", True))
        direction = kenburns.pick_variant(card.shot_index) if variety else "in"
        video_style = str(project.metadata.get("video_style", "")).lower()
        jitter = variety and video_style == "documentary"
        kenburns.build_kenburns_clip(
            Path(card.image_output), dst, project.settings.ffmpeg or "ffmpeg", seconds,
            direction=direction, jitter=jitter, cancel_check=cancel_check,
        )
        card.video_output = str(dst)
        card.route = "kenburns"
        card.status = "generated"
        card.approved = False
        qa = run_qa(
            pdir,
            card,
            settings,
            image=True,
            video=True,
            cancel_check=cancel_check,
        )
        variants: list[dict[str, Any]] = []
        if generated_image:
            variants.extend(
                _record_outputs(
                    pdir,
                    card_index,
                    card,
                    project,
                    source="ai",
                    mode="image",
                    output_fields=["image_output"],
                )
            )
        variants.extend(
            _record_outputs(
                pdir,
                card_index,
                card,
                project,
                source="kenburns",
                mode=mode,
                output_fields=["video_output"],
                metadata={"direction": direction, "jitter": jitter},
            )
        )
        _write_asset_board(pdir, cards)
        return {
            "ok": True,
            "card": card.__dict__,
            "kenburns": True,
            "variants": variants,
            "qa": qa,
        }

    # Image-only route: generate one still and let export hold it on screen.
    if (card.visual_type or "").lower() == "ai_image" and mode in {"route", "graphic"}:
        if not card.image_model:
            card.image_model = asset_board.preferred_realistic_image_model(project.settings.image_model_preference)
        img_path = comfy_generation.generate_comfy_image(
            comfy_url,
            pdir,
            card,
            cancel_check=cancel_check,
            settings=project.settings,
            reference_image=reference_image,
        )
        card.image_output = str(img_path)
        card.route = "image"
        card.status = "generated"
        card.approved = False
        qa = run_qa(
            pdir,
            card,
            project.settings,
            image=True,
            cancel_check=cancel_check,
        )
        variants = _record_outputs(
            pdir,
            card_index,
            card,
            project,
            source="ai",
            mode=mode,
            output_fields=["image_output"],
        )
        _write_asset_board(pdir, cards)
        return {
            "ok": True,
            "card": card.__dict__,
            "image": True,
            "variants": variants,
            "qa": qa,
        }

    if mode in {"route", "graphic"}:
        card.route = "image+video"
        card.visual_type = "ai_image_then_i2v"
        if not card.image_model:
            card.image_model = asset_board.preferred_realistic_image_model(project.settings.image_model_preference)
        if not card.video_model:
            card.video_model = "ltx2.3_i2v"
        if card.image_model:
            img_path = comfy_generation.generate_comfy_image(
                comfy_url,
                pdir,
                card,
                cancel_check=cancel_check,
                settings=project.settings,
                reference_image=reference_image,
            )
            card.image_output = str(img_path)
        if card.video_model:
            vid_path = comfy_generation.generate_comfy_ltx_video(
                comfy_url,
                pdir,
                card,
                draft=(False if force_full else None),
                cancel_check=cancel_check,
                settings=project.settings,
            )
            card.video_output = str(vid_path)
    elif mode == "image":
        # Phased queue: this card may arrive without a model (e.g. demoted from
        # stock) — resolve the default here, same as the route branch does.
        if not card.image_model:
            card.image_model = asset_board.preferred_realistic_image_model(project.settings.image_model_preference)
        img_path = comfy_generation.generate_comfy_image(
            comfy_url,
            pdir,
            card,
            cancel_check=cancel_check,
            settings=project.settings,
            reference_image=reference_image,
        )
        card.image_output = str(img_path)
        card.status = "image_ready"
        card.approved = False
        qa = run_qa(
            pdir,
            card,
            project.settings,
            image=True,
            cancel_check=cancel_check,
        )
        variants = _record_outputs(
            pdir,
            card_index,
            card,
            project,
            source="ai",
            mode=mode,
            output_fields=["image_output"],
        )
        _write_asset_board(pdir, cards)
        return {"ok": True, "card": card.__dict__, "variants": variants, "qa": qa}
    elif mode == "video":
        if not card.video_model:
            card.video_model = "ltx2.3_i2v"
        vid_path = comfy_generation.generate_comfy_ltx_video(
            comfy_url,
            pdir,
            card,
            draft=(False if force_full else None),
            cancel_check=cancel_check,
            settings=project.settings,
        )
        card.video_output = str(vid_path)
    else:
        raise ValueError(f"Unknown mode: {mode}")

    card.status = "generated"
    card.approved = False
    qa = run_qa(
        pdir,
        card,
        project.settings,
        image=mode in {"route", "graphic"} and bool(card.image_output),
        video=bool(card.video_output),
        cancel_check=cancel_check,
    )
    generated_fields = [
        field
        for field in ("image_output", "video_output")
        if str(getattr(card, field, "") or "").strip()
        and (mode in {"route", "graphic"} or field == f"{mode}_output")
    ]
    variants = _record_outputs(
        pdir,
        card_index,
        card,
        project,
        source="ai",
        mode=mode,
        output_fields=generated_fields,
    )
    _write_asset_board(pdir, cards)
    return {"ok": True, "card": card.__dict__, "variants": variants, "qa": qa}



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


def _lm_prompt(endpoint: str, model: str, prompt: str, ttl_seconds: int | None = None) -> dict[str, Any]:
    payload = {
        "model": model.strip() or "local-model",
        "messages": [
            {"role": "system", "content": "Return only valid JSON. No markdown."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
    }
    lm_client.add_ttl(payload, ttl_seconds)
    url = endpoint.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=lm_client.request_headers(url),
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
    delay = lm_client.unload_delay_seconds(settings)
    if delay is None:
        return {"ok": True, "skipped": True, "message": "LM Studio auto-unload disabled"}
    if delay > 0:
        return {"ok": True, "skipped": True, "delay": delay, "message": f"LM Studio TTL set ({delay}s)"}

    model = str(settings.lm_model or "").strip()
    if not model or model == "local-model":
        return {"ok": False, "skipped": True, "message": "LM Studio model is not configured"}
    try:
        result = lm_client.unload_model(settings.lm_url or lm_client.DEFAULT_URL, model)
        return {"ok": True, "skipped": False, "result": result}
    except Exception as exc:
        return {"ok": False, "skipped": False, "message": lm_client.explain_error(exc)}

def _regenerate_prompt_sync(pdir: Path, card_index: int, target: str) -> dict[str, Any]:
    project = open_project(pdir, load_settings())
    if not project:
        raise RuntimeError("Project not found")

    target = target if target in {"image_prompt", "video_prompt"} else "image_prompt"
    cards = _load_asset_board(pdir)
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

    ttl = lm_client.ttl_seconds(settings)
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
        new_prompt = compose_image_prompt(
            video_style,
            new_prompt,
            character_description=character_description,
            custom_notes=style_anchor,
        )
    else:
        new_prompt = compose_video_prompt(video_style, new_prompt, shot_description=card.description)

    setattr(card, target, new_prompt)
    _write_asset_board(pdir, cards)
    unload = _unload_lm_after_prompt(settings)
    return {"ok": True, "target": target, "prompt": new_prompt, "card": card.__dict__, "lm_unload": unload}

def _board_revision(pdir: Path) -> str:
    """Stable SHA-256 content revision, or ``0`` before the first board write."""
    path = pdir / "06_asset_board.json"
    if not path.exists():
        return "0"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _set_board_revision_headers(response: Response | None, pdir: Path) -> str:
    revision = _board_revision(pdir)
    if response is not None:
        response.headers["ETag"] = f'"{revision}"'
        response.headers["Cache-Control"] = "no-cache"
    return revision


def _if_match_satisfied(value: str, current_revision: str) -> bool:
    """Strong-match an If-Match value, accepting the old unquoted token too."""
    value = value.strip()
    if value == "*":
        return True
    current_etag = f'"{current_revision}"'
    for candidate in value.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            continue
        if candidate in {current_etag, current_revision}:
            return True
    return False


def _require_board_revision(request: Request, pdir: Path) -> str:
    """Require an optimistic-concurrency precondition for a full-board save."""
    current = _board_revision(pdir)
    if_match = request.headers.get("If-Match", "").strip()
    if not if_match:
        raise HTTPException(
            428,
            {
                "message": "Asset Board revision is required. Reload the board and retry with If-Match.",
                "current": current,
            },
        )
    if not _if_match_satisfied(if_match, current):
        raise HTTPException(
            409,
            {
                "message": "Доска изменилась (вероятно, завершилась генерация). "
                           "Обновите страницу и повторите — иначе новые кадры затрутся.",
                "expected": if_match,
                "current": current,
            },
        )
    return current


def _check_optional_board_revision(if_match: str | None, pdir: Path) -> None:
    """Apply optimistic concurrency to patch-style APIs when the client opts in."""
    # Direct unit calls receive FastAPI's Header descriptor as the Python
    # default; only an actual string represents a supplied HTTP precondition.
    if not isinstance(if_match, str) or not if_match:
        return
    current = _board_revision(pdir)
    if not _if_match_satisfied(if_match, current):
        raise HTTPException(
            409,
            {
                "message": "Asset Board changed since it was loaded. Reload it before editing.",
                "expected": if_match,
                "current": current,
            },
        )


def _require_card_operation(
    pdir: Path,
    card_index: int,
    card_id: str | None,
    if_match: str | None,
) -> None:
    """Bind a delayed long operation to both board revision and stable card ID."""
    _require_card_operation_headers(card_id, if_match)
    assert isinstance(card_id, str) and isinstance(if_match, str)
    _verify_card_operation_target(pdir, card_index, card_id, if_match)


def _require_card_operation_headers(
    card_id: str | None,
    if_match: str | None,
) -> None:
    if not isinstance(if_match, str) or not if_match.strip():
        raise HTTPException(
            428,
            "Asset Board revision is required. Reload the board and retry.",
        )
    if not isinstance(card_id, str) or not card_id.strip():
        raise HTTPException(
            428,
            "Stable X-Card-ID is required. Reload the board and retry.",
        )


def _verify_card_operation_target(
    pdir: Path,
    card_index: int,
    card_id: str,
    if_match: str,
) -> None:
    _check_optional_board_revision(if_match, pdir)
    cards = _load_asset_board(pdir)
    if card_index < 0 or card_index >= len(cards):
        raise HTTPException(404, "Card not found")
    current_id = str(getattr(cards[card_index], "card_id", "") or "").strip()
    if current_id != card_id.strip():
        raise HTTPException(
            409,
            {
                "message": "Card position changed since it was selected. Reload the board.",
                "expected_card_id": card_id.strip(),
                "current_card_id": current_id,
            },
        )


@contextmanager
def _user_board_mutation(pdir: Path, operation: str):
    """Atomically exclude long project jobs from one short HTTP board write."""
    try:
        with jobs.project_mutation(pdir.name, operation):
            yield
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))


@router.get("/{project_id:str}/asset-board")
async def get_asset_board(project_id: str, response: Response):
    pdir = _resolve_project(project_id)
    cards = _load_asset_board(pdir)
    # ETag lets the client detect a board changed under it (background render
    # writing image_output/video_output) and refuse a stale full-board save.
    _set_board_revision_headers(response, pdir)
    return [c.__dict__ for c in cards]


def _start_material_run(
    pdir: Path,
    policy: production.ProductionPolicy,
) -> str:
    def worker(cancel_event) -> None:
        material_engine.run_unattended_materials(
            pdir,
            pdir.name,
            policy,
            cancel_event,
            _generate_sync,
            service_check=lambda service: (
                _comfy_available(timeout=2.0) if service == "comfy" else True
            ),
        )

    return jobs.start_background_project_job(
        pdir.name,
        "Автономная сборка материалов",
        worker,
        resource="comfy",
    )


def _comfy_available(settings=None, *, timeout: float = 3.0) -> bool:
    settings = settings or load_settings()
    try:
        base = (settings.comfy_url or "http://127.0.0.1:8188").rstrip("/")
        # This check runs before every unattended operation, so it must remain
        # cheap.  On large custom-node installations /object_info can be
        # several megabytes and exceed this timeout even while ComfyUI is
        # healthy.  Workflow compatibility is validated separately before the
        # material job starts.
        with urllib.request.urlopen(f"{base}/system_stats", timeout=timeout):
            return True
    except Exception:
        return False


def _required_material_workflows(
    pdir: Path,
    settings,
) -> list[str]:
    required: set[str] = set()
    cards = _load_asset_board(pdir)
    for card in cards:
        data = card.__dict__
        visual_type = str(card.visual_type or "").lower()
        if (
            visual_type in {"ai_image", "still_kenburns", "ai_image_then_i2v"}
            and production.card_needs_image(data)
        ):
            image_model = str(card.image_model or "").lower()
            required.add("ideogram" if "ideogram" in image_model else "krea")
        if (
            visual_type == "ai_image_then_i2v"
            and production.card_needs_video(data)
        ):
            # The unattended Material action always passes force_full=True for
            # video operations. Preflight must validate the workflow that will
            # actually run, regardless of the manual LTX draft preference.
            required.add("ltx")
    reference_status = comfy_generation.reference_workflow_status(settings)
    if (
        reference_status.get("enabled")
        and canonical_references.reference_plan([card.__dict__ for card in cards])
    ):
        required.add("reference")
    return sorted(required)


def _material_workflow_preflight(pdir: Path, settings=None) -> dict[str, Any]:
    """Validate only workflows the pending material graph will actually use."""

    settings = settings or load_settings()
    required = _required_material_workflows(pdir, settings)
    if not required:
        return {"ok": True, "required_workflows": [], "workflows": {}}
    base = settings.comfy_url or "http://127.0.0.1:8188"
    try:
        object_info = comfy_client.comfy_request_json(
            f"{comfy_client.comfy_base_url(base)}/object_info",
            timeout=10,
        )
    except Exception as exc:
        return {
            "ok": False,
            "message": f"ComfyUI недоступен: {exc}",
            "required_workflows": required,
            "workflows": {},
        }
    paths = comfy_client.configured_workflow_paths(settings)
    workflows: dict[str, Any] = {
        name: comfy_client.validate_workflow_file(name, paths[name], object_info)
        for name in required
    }
    ok = all(item.get("ok") for item in workflows.values())
    return {
        "ok": ok,
        "message": (
            ""
            if ok
            else "Требуемые workflow ComfyUI отсутствуют или несовместимы."
        ),
        "required_workflows": required,
        "workflows": workflows,
    }


def resume_interrupted_material_runs() -> list[str]:
    """Resume crash-checkpointed unattended runs during application startup."""

    settings = load_settings()
    root = Path(settings.output_base or "projects").expanduser().resolve()
    resumed: list[str] = []
    if not root.is_dir():
        return resumed
    # Do not burn both attempts and replace every pending shot with a fallback
    # while the desktop is still bringing an external ComfyUI process online.
    if not _comfy_available(settings):
        return resumed
    for pdir in root.iterdir():
        if not pdir.is_dir():
            continue
        state = production.load_material_run(pdir)
        if not state or state.get("status") not in {"queued", "running", "interrupted"}:
            continue
        try:
            policy = production.ProductionPolicy.from_dict(state.get("policy"))
            if not policy.auto_resume:
                continue
            if not _material_workflow_preflight(pdir, settings).get("ok"):
                continue
            _start_material_run(pdir, policy)
            resumed.append(pdir.name)
        except (ValueError, jobs.ProjectBusy, RuntimeError):
            # A busy GPU or malformed legacy checkpoint must not prevent the
            # desktop application from starting. The run remains resumable via
            # the same Material button.
            continue
    return resumed


@router.get("/{project_id:str}/materials/preflight")
async def get_material_preflight(project_id: str):
    pdir = _resolve_project(project_id)
    return production.preflight_material_plan(pdir).to_dict()


@router.get("/{project_id:str}/materials/run")
async def get_material_run(project_id: str):
    pdir = _resolve_project(project_id)
    state = production.load_material_run(pdir)
    return state or {
        "schema_version": production.MATERIAL_RUN_SCHEMA_VERSION,
        "status": "not_started",
        "project_id": pdir.name,
    }


@router.post("/{project_id:str}/materials/run", status_code=202)
def start_material_run(project_id: str, request: MaterialRunRequest):
    # Plain ``def`` on purpose: the ComfyUI probes below block for seconds, and
    # FastAPI runs sync handlers in its threadpool instead of the event loop
    # that serves SSE streams and media.
    pdir = _resolve_project(project_id)
    try:
        policy = production.ProductionPolicy.from_dict(request.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    preflight = production.preflight_material_plan(pdir)
    if not preflight.allowed:
        raise HTTPException(409, preflight.to_dict())
    if "comfy" in preflight.required_services and not _comfy_available():
        raise HTTPException(
            503,
            {
                "message": (
                    "ComfyUI недоступен. Запустите ComfyUI и повторите; "
                    "движок не станет заменять всю очередь резервными кадрами."
                ),
                "service": "comfy",
                "preflight": preflight.to_dict(),
            },
        )
    if "comfy" in preflight.required_services:
        workflow_preflight = _material_workflow_preflight(pdir)
        if not workflow_preflight.get("ok"):
            raise HTTPException(
                503,
                {
                    "message": workflow_preflight.get("message"),
                    "service": "comfy",
                    "workflow_preflight": workflow_preflight,
                    "preflight": preflight.to_dict(),
                },
            )
    try:
        job_id = _start_material_run(pdir, policy)
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {
        "ok": True,
        "job_id": job_id,
        "preflight": preflight.to_dict(),
        "policy": request.model_dump(),
    }


@router.get("/{project_id:str}/asset-board/qa")
async def get_asset_qa(project_id: str):
    """Latest hash-bound image/video QA report for every stable card id."""
    pdir = _resolve_project(project_id)
    cards = _load_asset_board(pdir)
    current = {str(card.card_id): card for card in cards}
    latest = asset_qa_store.latest_reports(pdir)
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for card_id, by_kind in latest.items():
        card = current.get(card_id)
        result[card_id] = {}
        for kind, stored in by_kind.items():
            report = dict(stored)
            field = "image_output" if kind == "image" else "video_output"
            raw_path = Path(str(getattr(card, field, "") or "")) if card else Path()
            path = raw_path if raw_path.is_absolute() else pdir / raw_path
            try:
                current_hash = (
                    content_hash(path)
                    if path.is_file() and is_within(path, pdir)
                    else ""
                )
            except OSError:
                current_hash = ""
            expected_hash = str((report.get("media") or {}).get("sha256", ""))
            report["stale"] = not current_hash or current_hash != expected_hash
            result[card_id][kind] = report
    store = asset_qa_store.load_store(pdir)
    return {
        "schema_version": int(store.get("schema_version", 0) or 0),
        "revision": int(store.get("revision", 0) or 0),
        "history_count": len(store.get("reports", [])),
        "reports": result,
    }


@router.post("/{project_id:str}/asset-board/save")
async def save_asset_board(project_id: str, data: list[AssetCardSchema], request: Request,
                           response: Response):
    pdir = _resolve_project(project_id)
    # A full-board save overwrites everything, so hold the same lock used by
    # render workers for the revision check, variant snapshot, and final write.
    with _user_board_mutation(pdir, "Сохранение Asset Board"):
        # If-Match must equal the current revision, else the client's copy is
        # stale and would clobber fresh render outputs (P0-4).
        _require_board_revision(request, pdir)
        previous = _load_asset_board(pdir)
        cards = _cards_from_api(data, previous)
        _validate_user_media_paths(pdir, previous, cards)
        _record_manual_changes(pdir, previous, cards, mode="bulk_save")
        _write_asset_board(pdir, cards)
        revision = _set_board_revision_headers(response, pdir)
        return {"ok": True, "count": len(cards), "rev": revision}


@router.post("/{project_id:str}/asset-board/rebuild")
async def rebuild_asset_board(project_id: str, response: Response = None):
    pdir = _resolve_project(project_id)
    try:
        def run():
            with jobs.project_job(pdir.name, "Пересборка Asset Board") as cancel_event:
                previous = _load_asset_board(pdir)
                jobs.check(cancel_event)
                result = _build_asset_board(pdir)
                jobs.check(cancel_event)
                _record_manual_changes(pdir, previous, result, mode="rebuild")
                jobs.check(cancel_event)
                _write_asset_board(pdir, result)
                return result
        cards = await asyncio.to_thread(run)
        _set_board_revision_headers(response, pdir)
        return {"ok": True, "count": len(cards), "cards": [c.__dict__ for c in cards]}
    except (jobs.ProjectBusy, jobs.JobCancelled) as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.put("/{project_id:str}/asset-board/{card_index}")
async def update_card(
    project_id: str,
    card_index: int,
    data: dict[str, Any],
    response: Response = None,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    pdir = _resolve_project(project_id)
    with _user_board_mutation(pdir, "Правка карточки Asset Board"):
        _check_optional_board_revision(if_match, pdir)
        cards = _load_asset_board(pdir)

        if card_index < 0 or card_index >= len(cards):
            raise HTTPException(404, "Card not found")

        previous_cards = list(cards)
        card = cards[card_index]
        previous_card = card
        if "card_id" in data and str(data["card_id"] or "").strip() != card.card_id:
            raise HTTPException(409, "AssetCard card_id is immutable")
        unknown = sorted(set(data) - set(AssetCardSchema.model_fields))
        if unknown:
            raise HTTPException(422, {"message": "Unknown AssetCard fields", "fields": unknown})
        try:
            validated = AssetCardSchema.model_validate({**card.__dict__, **data})
        except ValidationError as exc:
            raise HTTPException(422, exc.errors())
        card = CoreAssetCard(**validated.model_dump())
        cards[card_index] = card
        _sync_asset_card_route(card)
        _validate_user_media_paths(pdir, previous_cards, cards)

        project = open_project(pdir, load_settings())
        if not project:
            raise HTTPException(404, "Project not found")
        _record_manual_card_change(
            pdir,
            card_index,
            previous_card,
            card,
            project,
            mode="card_update",
        )
        _write_asset_board(pdir, cards)
        _set_board_revision_headers(response, pdir)
        return card.__dict__


@router.get("/{project_id:str}/asset-board/{card_index}")
async def get_card(project_id: str, card_index: int):
    pdir = _resolve_project(project_id)
    cards = _load_asset_board(pdir)
    if card_index < 0 or card_index >= len(cards):
        raise HTTPException(404, "Card not found")
    return cards[card_index].__dict__


@router.post("/{project_id:str}/asset-board/{card_index}/qa")
async def run_card_qa(
    project_id: str,
    card_index: int,
    response: Response = None,
    if_match: str | None = Header(default=None, alias="If-Match"),
    card_id: str | None = Header(default=None, alias="X-Card-ID"),
):
    """Explicitly re-run QA for the current media without regenerating it."""
    pdir = _resolve_project(project_id)
    _require_card_operation_headers(card_id, if_match)
    try:
        def run():
            with jobs.project_job(
                pdir.name,
                f"QA кадра {card_index + 1}",
                resource="llm",
            ) as cancel_event:
                _require_card_operation(pdir, card_index, card_id, if_match)
                cards = _load_asset_board(pdir)
                card = cards[card_index]
                project = open_project(pdir, load_settings())
                if not project:
                    raise RuntimeError("Project not found")

                def check_cancelled() -> None:
                    jobs.check(cancel_event)

                reports: list[dict[str, Any]] = []
                if str(card.image_output or "").strip():
                    reports.append(asset_qa.inspect_image(
                        pdir, card, project.settings, cancel_check=check_cancelled,
                    ))
                if str(card.video_output or "").strip():
                    reports.append(asset_qa.inspect_video(
                        pdir, card, project.settings, cancel_check=check_cancelled,
                    ))
                if not reports:
                    raise ValueError("У карточки нет изображения или видео для QA")
                previous_status = card.status
                previous_notes = card.notes
                asset_qa.apply_review_mode(card, reports)
                if card.status != previous_status or card.notes != previous_notes:
                    _write_asset_board(pdir, cards)
                jobs.check(cancel_event)
                return {"ok": True, "card": card.__dict__, "qa": reports}

        result = await asyncio.to_thread(run)
        _set_board_revision_headers(response, pdir)
        return result
    except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
        raise HTTPException(409, str(exc)) from exc
    except asset_qa_store.QAStoreIntegrityError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get(
    "/{project_id:str}/asset-board/{card_index}/variants",
    response_model=AssetVariantList,
)
async def get_card_variants(project_id: str, card_index: int):
    pdir = _resolve_project(project_id)
    cards = _load_asset_board(pdir)
    if card_index < 0 or card_index >= len(cards):
        raise HTTPException(404, "Card not found")
    return asset_variants.card_variant_payload(
        pdir,
        card_index,
        shot_index=cards[card_index].shot_index,
        card_id=cards[card_index].card_id,
    )


@router.post(
    "/{project_id:str}/asset-board/{card_index}/variants/{variant_id}/select",
    response_model=AssetVariantSelectResult,
)
async def select_card_variant(
    project_id: str,
    card_index: int,
    variant_id: str,
    response: Response = None,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    pdir = _resolve_project(project_id)
    with _user_board_mutation(pdir, "Выбор версии ассета"):
        _check_optional_board_revision(if_match, pdir)
        cards = _load_asset_board(pdir)
        if card_index < 0 or card_index >= len(cards):
            raise HTTPException(404, "Card not found")
        card = cards[card_index]
        try:
            candidate = asset_variants.get_variant(pdir, variant_id)
            candidate_card_id = str(candidate.get("card_id", "") or "")
            if candidate_card_id:
                belongs = candidate_card_id == card.card_id
            else:
                # Schema-v1 records have only their historical board position.
                belongs = int(candidate.get("card_index", -1)) == card_index
            if not belongs:
                raise HTTPException(404, "Variant does not belong to this card")
            variant = asset_variants.select_variant(
                pdir,
                variant_id,
                reason="api",
                card_id=card.card_id,
                card_index=card_index,
            )
        except asset_variants.VariantNotFound as exc:
            raise HTTPException(404, str(exc))
        except asset_variants.VariantIntegrityError as exc:
            raise HTTPException(409, str(exc))

        output_field = str(variant["output_field"])
        setattr(card, output_field, variant["file_path"])
        metadata = variant.get("metadata") if isinstance(variant.get("metadata"), dict) else {}
        for field in (
            "route",
            "visual_type",
            "source_url",
            "source_license",
            "source_author",
            "stock_score",
            "reference_url",
            "reference_source",
            "reference_credit",
            "qa_score",
            "ltx_draft",
        ):
            if field in metadata and hasattr(card, field):
                setattr(card, field, metadata[field])
        if output_field == "image_output" and not card.video_output:
            card.status = "image_ready"
        else:
            card.status = "generated"
        card.approved = False
        _write_asset_board(pdir, cards)
        _set_board_revision_headers(response, pdir)
        return {
            "ok": True,
            "card": card.__dict__,
            "variant": variant,
            "selections": asset_variants.current_selections(
                pdir, card_index, card_id=card.card_id
            ),
        }



@router.post("/{project_id:str}/asset-board/{card_index}/regenerate-prompt")
async def regenerate_card_prompt(
    project_id: str,
    card_index: int,
    req: RegeneratePromptRequest,
    response: Response = None,
    if_match: str | None = Header(default=None, alias="If-Match"),
    card_id: str | None = Header(default=None, alias="X-Card-ID"),
):
    pdir = _resolve_project(project_id)
    _require_card_operation_headers(card_id, if_match)
    try:
        def run():
            with jobs.project_job(pdir.name, "Перегенерация промпта", resource="llm") as cancel_event:
                _require_card_operation(pdir, card_index, card_id, if_match)
                jobs.check(cancel_event)
                result = _regenerate_prompt_sync(pdir, card_index, req.target)
                jobs.check(cancel_event)
                return result
        result = await asyncio.to_thread(run)
        _set_board_revision_headers(response, pdir)
        return result
    except (jobs.ProjectBusy, jobs.JobCancelled) as e:
        raise HTTPException(409, str(e))
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))

def _stock_sync(pdir: Path, card_index: int) -> dict[str, Any]:
    """Resolve one card to a stock clip. On success writes the clip into the
    board; on no-match leaves the card untouched so the user can send it to AI."""
    settings = load_settings()
    cards = _load_asset_board(pdir)
    if card_index < 0 or card_index >= len(cards):
        raise RuntimeError("Card not found")

    card = cards[card_index]
    logs: list[str] = []
    result = stock_lib.fetch_stock_for_card(pdir, card, settings, log=logs.append)
    if not result:
        reason = logs[-1] if logs else "Подходящий сток не найден"
        return {"ok": False, "message": reason, "logs": logs, "card": card.__dict__}

    card.video_output = result["video_output"]
    card.source_url = result.get("source_url", "")
    card.source_license = result.get("license", "")
    card.source_author = result.get("author", "")
    card.stock_score = float(result.get("stock_score", 0.0))
    card.visual_type = "stock_video"
    card.route = "stock"
    card.status = "generated"
    card.approved = False
    project = open_project(pdir, settings)
    if not project:
        raise RuntimeError("Project not found")
    variants = _record_outputs(
        pdir,
        card_index,
        card,
        project,
        source="stock",
        mode="stock",
        output_fields=["video_output"],
        metadata={"provider": result.get("source", "")},
    )
    _write_asset_board(pdir, cards)
    return {"ok": True, "card": card.__dict__, "source": result.get("source", ""),
            "score": card.stock_score, "variants": variants}


@router.post("/{project_id:str}/asset-board/{card_index}/stock")
async def resolve_stock(
    project_id: str,
    card_index: int,
    response: Response = None,
    if_match: str | None = Header(default=None, alias="If-Match"),
    card_id: str | None = Header(default=None, alias="X-Card-ID"),
):
    pdir = _resolve_project(project_id)
    _require_card_operation_headers(card_id, if_match)
    try:
        def run():
            with jobs.project_job(pdir.name, "Поиск стокового кадра", resource="stock") as cancel_event:
                _require_card_operation(pdir, card_index, card_id, if_match)
                jobs.check(cancel_event)
                result = _stock_sync(pdir, card_index)
                jobs.check(cancel_event)
                return result
        result = await asyncio.to_thread(run)
        _set_board_revision_headers(response, pdir)
        return result
    except (jobs.ProjectBusy, jobs.JobCancelled) as e:
        raise HTTPException(409, str(e))
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/{project_id:str}/generate/{card_index}")
async def generate_card(project_id: str, card_index: int,
                        mode: Literal["route", "graphic", "image", "video"] = "route",
                        skip_stock: bool = False, force_full: bool = False,
                        defer_qa: bool = False,
                        response: Response = None,
                        if_match: str | None = Header(default=None, alias="If-Match"),
                        card_id: str | None = Header(default=None, alias="X-Card-ID")):
    pdir = _resolve_project(project_id)
    _require_card_operation_headers(card_id, if_match)
    try:
        def run():
            with jobs.project_job(pdir.name, f"Генерация кадра {card_index + 1}", resource="comfy") as cancel_event:
                _require_card_operation(pdir, card_index, card_id, if_match)
                jobs.check(cancel_event)

                def check_cancelled() -> bool:
                    # ``jobs.check`` raises JobCancelled.  Returning False keeps
                    # the callback compatible with core.comfy's optional
                    # truthy-return cancellation convention.
                    jobs.check(cancel_event)
                    return False

                result = _generate_sync(
                    pdir,
                    card_index,
                    mode,
                    skip_stock,
                    force_full,
                    cancel_check=check_cancelled,
                    defer_qa=defer_qa,
                )
                jobs.check(cancel_event)
                return result
        result = await asyncio.to_thread(run)
        _set_board_revision_headers(response, pdir)
        return result
    except (jobs.ProjectBusy, jobs.JobCancelled) as e:
        raise HTTPException(409, str(e))
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        # The worker has released project_job by this point. Reacquire the same
        # lock before annotating the board so a newly started render can never
        # have its result overwritten by this older error path.
        try:
            with jobs.project_mutation(pdir.name, "Запись ошибки генерации"):
                cards = _load_asset_board(pdir)
                if card_index < len(cards):
                    cards[card_index].status = "needs_review"
                    cards[card_index].notes = (
                        (cards[card_index].notes + "\n")
                        if cards[card_index].notes
                        else ""
                    ) + f"Generation error: {e}"
                    _write_asset_board(pdir, cards)
        except Exception:
            pass
        raise HTTPException(500, str(e))


# Remotion rendering and the /render composition endpoint were removed:
# the pipeline is now pure AI video (ComfyUI image -> LTX i2v), assembled by
# the ffmpeg export on the Preview page.
