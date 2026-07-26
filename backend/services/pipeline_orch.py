from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from core import board as board_store
from core import lm as lm_client
from core import scenes as scene_tools
from core import shot_planning as shot_planner
from core import shots as shot_tools
from core.models import Scene, TranscriptSegment
from core.project import atomic_write_text


def _to_card(c: Any) -> dict[str, Any]:
    d = c.__dict__ if hasattr(c, "__dict__") else {}
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _to_cards(cards: list) -> list[dict[str, Any]]:
    return [_to_card(c) for c in cards]


def _lm_unload_delay_seconds(settings: dict) -> int | None:
    if not settings.get("lm_auto_unload", True):
        return None
    try:
        return max(0, int(str(settings.get("lm_unload_delay", "0")).strip() or "0"))
    except ValueError:
        return 60


def _ensure_lm_context(settings: dict, log: Callable[[str], None]):
    base_url = settings.get("lm_url", "http://127.0.0.1:1234/v1")
    model = str(settings.get("lm_model", "local-model")).strip()
    ctx_raw = settings.get("lm_context", "32768")
    try:
        context_length = int(str(ctx_raw).strip())
    except ValueError:
        context_length = 32768
    if not model or model == "local-model":
        log("LM Studio context set skipped: no model name configured")
        return
    try:
        log(f"Setting LM Studio context to {context_length} for model {model}...")
        result = lm_client.load_model(base_url, model, context_length)
        log(f"LM Studio context updated: {json.dumps(result, ensure_ascii=False)}")
    except Exception as exc:
        log(f"LM Studio context warning (non-fatal): {lm_client.explain_error(exc)}")


def _maybe_unload_lm(settings: dict, reason: str, log: Callable[[str], None], model: str | None = None):
    delay = _lm_unload_delay_seconds(settings)
    if delay is None:
        log("LM Studio auto-unload disabled")
        return
    if delay > 0:
        log(f"LM Studio TTL set ({delay}s) — model stays loaded")
        return
    base_url = settings.get("lm_url", "http://127.0.0.1:1234/v1")
    model = str(model if model is not None else settings.get("lm_model", "local-model")).strip()
    if not model:
        log("LM Studio unload skipped: empty model id")
        return
    try:
        log(f"LM Studio unload: model={model}, reason={reason}")
        result = lm_client.unload_model(base_url, model)
        log(f"LM Studio unload result: {json.dumps(result, ensure_ascii=False)}")
    except Exception as exc:
        log(f"LM Studio unload error: {lm_client.explain_error(exc)}")


def run_scene_analysis(
    project_dir: Path,
    segments_data: list[dict[str, Any]],
    settings: dict[str, Any],
    log_callback: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    base_url = settings.get("lm_url", "http://127.0.0.1:1234/v1")
    model = settings.get("lm_model", "local-model")
    ttl_seconds = _lm_unload_delay_seconds(settings)
    ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else None

    style_anchor = settings.get("style_anchor", "")
    character_description = settings.get("character_description", "")
    video_style = settings.get("video_style", "knitted")

    t_segments = [TranscriptSegment(s["start"], s["end"], s["text"]) for s in segments_data]

    embed_model = settings.get("lm_embed_model", "text-embedding-nomic-embed-text-v1.5")
    seg_min = int(settings.get("seg_min_seconds", 20) or 20)
    seg_max = int(settings.get("seg_max_seconds", 90) or 90)
    seg_sensitivity = float(settings.get("seg_sensitivity", 0.6) or 0.6)
    seg_target = int(settings.get("seg_target_scenes", 0) or 0)

    # AUTHORED scenes first: if the project has a script (00_script.json), the
    # narration was literally written as beats — align them to the transcript
    # and use the author's boundaries/titles instead of guessing statistically.
    from core import script_align
    authored = script_align.scenes_from_script(
        project_dir,
        t_segments,
        Scene,
        seg_min,
        seg_max,
    )
    if authored:
        log(
            f"Авторские главы выровнены по аудио и разбиты на "
            f"{len(authored)} монтажных сцен ({seg_min}–{seg_max} с)."
        )
        try:
            _ensure_lm_context(settings, log)
            authored = scene_tools.enrich_scenes_with_lm(
                authored, base_url, model, ttl_seconds=ttl,
                style_anchor=style_anchor,
                character_description=character_description,
                video_style=video_style,
                log_callback=log,
                require_all=True,
                allow_plain_json_fallback=True,
            )
        finally:
            _maybe_unload_lm(settings, "scene analysis finished (script beats)", log)
        return [{"index": s.index, "start": s.start, "end": s.end, "title": s.title,
                 "voice_text": s.voice_text, "visual_direction": s.visual_direction,
                 "screen_text": s.screen_text, "music_note": s.music_note,
                 "retention_note": s.retention_note} for s in authored]

    # PRIMARY: deterministic scene boundaries via embeddings, then the LLM only
    # LABELS those fixed scenes (title/visual_direction/etc). This stops the
    # boundaries from "jumping" run to run.
    try:
        log("Сегментация сцен по эмбеддингам...")
        try:
            scenes = scene_tools.build_scenes_by_embedding(
                t_segments, base_url, embed_model, fallback=False, ttl_seconds=ttl,
                min_seconds=seg_min, max_seconds=seg_max, sensitivity=seg_sensitivity, target_scenes=seg_target,
                log_callback=log,
            )
        finally:
            # The embeddings endpoint may have loaded a model even when the
            # request later failed. Always apply the unload policy before the
            # chat fallback so both models do not compete for VRAM.
            _maybe_unload_lm(
                settings, "embedding segmentation finished", log, model=embed_model,
            )
        log(f"Границы по эмбеддингам: {len(scenes)} сцен; озаглавливаю через LLM...")
        _ensure_lm_context(settings, log)
        scenes = scene_tools.enrich_scenes_with_lm(
            scenes, base_url, model, ttl_seconds=ttl,
            style_anchor=style_anchor,
            character_description=character_description,
            video_style=video_style,
            log_callback=log,
            require_all=True,
            allow_plain_json_fallback=True,
        )
    except Exception as exc:
        # No embedding model available — fall back to LLM grouping, then duration.
        log(f"Эмбеддинги недоступны ({exc}); группирую сцены через LLM...")
        try:
            _ensure_lm_context(settings, log)
            scenes = scene_tools.call_lm_studio_for_segments(
                base_url, model, t_segments, ttl_seconds=ttl,
                style_anchor=style_anchor,
                character_description=character_description,
                video_style=video_style,
            )
        except Exception as exc2:
            raise RuntimeError(
                f"LM Studio не смогла выполнить анализ сцен: {exc2}"
            ) from exc2

    _maybe_unload_lm(settings, "scene analysis finished", log)

    result = [{"index": s.index, "start": s.start, "end": s.end, "title": s.title,
               "voice_text": s.voice_text, "visual_direction": s.visual_direction,
               "screen_text": s.screen_text, "music_note": s.music_note,
               "retention_note": s.retention_note} for s in scenes]
    return result


def run_shot_generation(
    project_dir: Path,
    scenes_data: list[dict[str, Any]],
    settings: dict[str, Any],
    log_callback: Callable[[str], None] | None = None,
    on_scene_shots: Callable[[list[Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    pipeline_scenes = [Scene(**s) for s in scenes_data]
    image_pref = settings.get("image_model_preference", "Auto")
    base_url = settings.get("lm_url", "http://127.0.0.1:1234/v1")
    model = settings.get("lm_model", "local-model")
    ttl_seconds = _lm_unload_delay_seconds(settings)
    ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else None

    style_anchor = settings.get("style_anchor", "")
    character_description = settings.get("character_description", "")
    video_style = settings.get("video_style", "knitted")

    segments_path = project_dir / "01_transcript_segments.json"
    if segments_path.exists():
        seg_data = json.loads(segments_path.read_text("utf-8"))
        pipeline_segments = [TranscriptSegment(s["start"], s["end"], s["text"]) for s in seg_data]
    else:
        pipeline_segments = []

    # Shot mode: "ai" (all AI, default), "stock" (all stock), "hybrid" (LLM picks
    # per shot). Fall back to the legacy stock_mode flag for old projects.
    shot_mode = str(settings.get("shot_mode") or ("stock" if settings.get("stock_mode") else "ai")).lower()
    route_priority = str(settings.get("route_priority") or "balanced").lower()

    _ensure_lm_context(settings, log)
    log(f"Generating shot list via LM Studio (mode={shot_mode}, route_priority={route_priority})...")
    reference_density = (
        str(settings.get("reference_density", "normal") or "normal")
        if settings.get("reference_enable", True)
        else "off"
    )
    global_visual_plan: list[dict[str, object]] = []

    def capture_visual_plan(items: list[dict[str, object]]) -> None:
        global_visual_plan[:] = items

    shots = shot_planner.call_lm_studio_for_shots(
        base_url, model, pipeline_scenes, image_pref,
        segments=pipeline_segments, ttl_seconds=ttl,
        on_scene_shots=on_scene_shots,
        style_anchor=style_anchor,
        character_description=character_description,
        video_style=video_style,
        log_callback=log,
        hybrid=(shot_mode == "hybrid"),
        route_priority=route_priority,
        # AI mode with distribution: kenburns/LTX mix under the same priority
        # selector (no stock — the style stays fully generated).
        ai_route_mix=(shot_mode == "ai"),
        reference_density=reference_density,
        on_visual_plan=capture_visual_plan,
    )
    if not global_visual_plan:
        raise ValueError("LM Studio не вернула обязательный глобальный визуальный план")
    atomic_write_text(
        project_dir / "03_global_visual_plan.json",
        json.dumps(global_visual_plan, ensure_ascii=False, indent=2),
    )
    log(
        f"Глобальный визуальный план сохранён: "
        f"{project_dir / '03_global_visual_plan.json'}"
    )

    _maybe_unload_lm(settings, "shot list generation finished", log)

    cut = int(settings.get("stock_cut_seconds", 5) or 5)
    variable = bool(settings.get("stock_variable_rhythm", True))
    if shot_mode == "stock":
        shots = shot_tools.split_shots_for_stock(
            shots, cut, segments=pipeline_segments, variable=variable,
        )
        log(f"Стоковый режим: кадры разбиты на куски ≤{cut}с"
            + (" (переменный ритм, каты по границам фраз)" if variable else "")
            + f", всего кадров: {len(shots)}")
    elif shot_mode == "hybrid":
        shots = shot_tools.split_stock_shots_only(
            shots, cut, segments=pipeline_segments, variable=variable,
        )
        n_stock = sum(1 for s in shots if (s.visual_type or '').lower() == 'stock_video')
        log(f"Гибрид: сток-кадры разбиты на ≤{cut}с; всего кадров {len(shots)} (стоковых {n_stock})")

    # A stock cut is a timeline decision, not a new visual concept.  Collapse
    # identical pieces back to one source card, then remove slots too short to
    # justify their generation.  Export still trims that source to the exact
    # final timeline range.
    shots = shot_tools.collapse_redundant_shot_runs(
        shots,
        log_callback=log,
    )
    shots = shot_tools.enforce_minimum_shot_durations(
        shots,
        pipeline_scenes,
        log_callback=log,
    )

    log("Building asset board...")
    cards = board_store.build_asset_board(project_dir, shots=shots)

    shots_out = [{"index": s.index, "scene_index": s.scene_index,
                  "start": s.start, "end": s.end, "visual_type": s.visual_type,
                  "description": s.description, "asset_query": s.asset_query,
                  "image_model": s.image_model, "video_model": s.video_model,
                  "image_prompt": s.image_prompt, "video_prompt": s.video_prompt,
                  "screen_text": s.screen_text, "motion": s.motion,
                  "priority": s.priority, "fallback": s.fallback,
                  "sfx_query": getattr(s, "sfx_query", ""),
                  "sfx_accent_query": getattr(s, "sfx_accent_query", "")} for s in shots]

    return shots_out, _to_cards(cards)
