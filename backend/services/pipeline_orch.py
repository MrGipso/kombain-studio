from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from core import board as board_store
from core import continuity
from core import lm as lm_client
from core import scenes as scene_tools
from core import shot_planning as shot_planner
from core import shots as shot_tools
from core.models import Scene, Shot, TranscriptSegment
from core.project import atomic_write_text, content_hash


_GLOBAL_VISUAL_PLAN_CACHE_VERSION = 1
_GLOBAL_VISUAL_PLAN_CACHE = ".global_visual_plan_cache.json"
SHOT_STAGE_SETTING_KEYS = (
    "lm_model",
    "lm_context",
    "video_style",
    "style_anchor",
    "character_description",
    "shot_mode",
    "route_priority",
    "stock_mode",
    "stock_cut_seconds",
    "stock_variable_rhythm",
    "image_model_preference",
    "reference_enable",
    "reference_density",
)
_LEGACY_SHOT_STAGE_SETTING_KEYS = tuple(
    key
    for key in SHOT_STAGE_SETTING_KEYS
    if key not in {"stock_variable_rhythm", "reference_enable", "reference_density"}
)


def _to_card(c: Any) -> dict[str, Any]:
    d = c.__dict__ if hasattr(c, "__dict__") else {}
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _to_cards(cards: list) -> list[dict[str, Any]]:
    return [_to_card(c) for c in cards]


def _global_visual_plan_cache_key(
    scenes_data: list[dict[str, Any]],
    settings: dict[str, Any],
) -> str:
    return content_hash({
        "version": _GLOBAL_VISUAL_PLAN_CACHE_VERSION,
        "scenes": scenes_data,
        "director": {
            key: settings.get(key)
            for key in (
                "lm_model",
                "video_style",
                "style_anchor",
                "character_description",
            )
        },
    })


def shot_stage_input_hash(
    scenes_data: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    setting_keys: tuple[str, ...] = SHOT_STAGE_SETTING_KEYS,
) -> str:
    """Input identity of the shots stage.

    The shots route stores it in the scene checkpoint and the stage manifest,
    and the global-plan cache compares against it, so all of them must use
    this one definition.
    """
    return content_hash({
        "scenes": scenes_data,
        "settings": {
            key: settings.get(key) for key in setting_keys
        },
    })


def _legacy_plan_matches_completed_stage(
    project_dir: Path,
    scenes_data: list[dict[str, Any]],
    settings: dict[str, Any],
) -> bool:
    manifest_path = project_dir / ".pipeline_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
        stage = manifest.get("stages", {}).get("shots", {})
    except (OSError, ValueError, TypeError):
        return False
    if stage.get("status") != "completed":
        return False
    known_hashes = {
        shot_stage_input_hash(scenes_data, settings),
        shot_stage_input_hash(
            scenes_data,
            settings,
            setting_keys=_LEGACY_SHOT_STAGE_SETTING_KEYS,
        ),
    }
    return stage.get("input_hash") in known_hashes


def _load_cached_global_visual_plan(
    project_dir: Path,
    scenes_data: list[dict[str, Any]],
    scenes: list[Scene],
    settings: dict[str, Any],
    log: Callable[[str], None],
) -> list[dict[str, object]] | None:
    plan_path = project_dir / "03_global_visual_plan.json"
    meta_path = project_dir / _GLOBAL_VISUAL_PLAN_CACHE
    if not plan_path.is_file():
        return None
    expected_key = _global_visual_plan_cache_key(scenes_data, settings)
    metadata_matches = False
    try:
        metadata = json.loads(meta_path.read_text("utf-8"))
        metadata_matches = (
            metadata.get("schema_version") == _GLOBAL_VISUAL_PLAN_CACHE_VERSION
            and metadata.get("cache_key") == expected_key
        )
    except (OSError, ValueError, TypeError):
        metadata_matches = False
    if not metadata_matches and not _legacy_plan_matches_completed_stage(
        project_dir,
        scenes_data,
        settings,
    ):
        return None
    try:
        raw = json.loads(plan_path.read_text("utf-8"))
        parsed = shot_planner.stabilize_global_visual_context(
            shot_planner.parse_global_visual_plan(
                json.dumps(raw, ensure_ascii=False),
                scenes,
            )
        )
    except (OSError, ValueError, TypeError) as exc:
        log(f"Кэш глобального плана пропущен: {exc}")
        return None
    if not metadata_matches:
        log(
            "Существующий глобальный план совпадает с завершённой "
            "версией сцен и будет принят как кэш."
        )
    return [item.as_dict() for item in parsed]


def _ensure_lm_context(
    settings: dict,
    log: Callable[[str], None],
) -> str | None:
    if not lm_client.is_lmstudio(settings):
        log("Облачный LLM API: установка контекста LM Studio пропущена")
        return None
    base_url = settings.get("lm_url", "http://127.0.0.1:1234/v1")
    model = str(settings.get("lm_model", "local-model")).strip()
    ctx_raw = settings.get("lm_context", "32768")
    try:
        context_length = int(str(ctx_raw).strip())
    except ValueError:
        context_length = 32768
    if not model or model == "local-model":
        log("LM Studio context set skipped: no model name configured")
        return None
    try:
        log(f"Setting LM Studio context to {context_length} for model {model}...")
        result = lm_client.load_model(base_url, model, context_length)
        log(f"LM Studio context updated: {json.dumps(result, ensure_ascii=False)}")
        return str(result.get("instance_id", "") or "").strip() or None
    except Exception as exc:
        log(f"LM Studio context warning (non-fatal): {lm_client.explain_error(exc)}")
        return None


def _maybe_unload_lm(settings: dict, reason: str, log: Callable[[str], None], model: str | None = None):
    delay = lm_client.unload_delay_seconds(settings)
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
    ttl = lm_client.ttl_seconds(settings)

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

    # Chapter boundaries come from the author, but the cuts INSIDE a chapter
    # are still placed semantically: a duration-only split puts a cut every
    # target_seconds regardless of what is being said. Degrades to the duration
    # partitioner by itself when the embedding endpoint is unavailable.
    def _semantic_split(segs, *, min_seconds, max_seconds, target_seconds):
        return scene_tools.partition_segments_by_embedding(
            segs,
            base_url,
            embed_model,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            target_seconds=target_seconds,
            sensitivity=seg_sensitivity,
            ttl_seconds=ttl,
            log_callback=log,
        )

    authored = script_align.scenes_from_script(
        project_dir,
        t_segments,
        Scene,
        seg_min,
        seg_max,
        partitioner=_semantic_split,
    )
    if authored:
        log(
            f"Границы глав взяты из авторского скрипта; резки внутри глав — "
            f"по эмбеддингам ({embed_model}). Получилось {len(authored)} "
            f"монтажных сцен ({seg_min}–{seg_max} с)."
        )
        _maybe_unload_lm(
            settings, "chapter segmentation finished", log, model=embed_model,
        )
        try:
            _ensure_lm_context(settings, log)
            authored = scene_tools.enrich_scenes_with_lm(
                authored, base_url, model, ttl_seconds=ttl,
                style_anchor=style_anchor,
                character_description=character_description,
                video_style=video_style,
                log_callback=log,
                # See the embedding path below: labelling failures must not
                # discard already-aligned scene boundaries.
                require_all=False,
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
            # Labels are cosmetic; scene BOUNDARIES are the valuable output and
            # they are already fixed at this point. Raising here used to throw
            # away a good embedding segmentation over one over-long caption and
            # fall through to an entirely different (worse) grouping strategy.
            # Unlabelled scenes keep their deterministic heuristic titles.
            require_all=False,
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
    resume_shots: list[dict[str, Any]] | list[Shot] | None = None,
    on_progress: Callable[[str, float], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    pipeline_scenes = [Scene(**s) for s in scenes_data]
    image_pref = settings.get("image_model_preference", "Auto")
    base_url = settings.get("lm_url", "http://127.0.0.1:1234/v1")
    model = settings.get("lm_model", "local-model")
    ttl = lm_client.ttl_seconds(settings)

    style_anchor = settings.get("style_anchor", "")
    character_description = settings.get("character_description", "")
    video_style = settings.get("video_style", "knitted")

    segments_path = project_dir / "01_transcript_segments.json"
    if segments_path.exists():
        seg_data = json.loads(segments_path.read_text("utf-8"))
        pipeline_segments = [TranscriptSegment(s["start"], s["end"], s["text"]) for s in seg_data]
    else:
        pipeline_segments = []

    pipeline_resume_shots: list[Shot] = []
    for item in resume_shots or []:
        try:
            pipeline_resume_shots.append(
                item if isinstance(item, Shot) else Shot(**item)
            )
        except (TypeError, ValueError):
            # A stale/corrupt checkpoint must never poison a fresh generation.
            pipeline_resume_shots = []
            log("Контрольная точка кадров повреждена; начинаю сцены заново.")
            break

    # Shot mode: "ai" (all AI, default), "stock" (all stock), "hybrid" (LLM picks
    # per shot). Fall back to the legacy stock_mode flag for old projects.
    shot_mode = str(settings.get("shot_mode") or ("stock" if settings.get("stock_mode") else "ai")).lower()
    route_priority = str(settings.get("route_priority") or "balanced").lower()

    loaded_model_instance = _ensure_lm_context(settings, log)
    log(f"Generating shot list via LM Studio (mode={shot_mode}, route_priority={route_priority})...")
    reference_density = (
        str(settings.get("reference_density", "normal") or "normal")
        if settings.get("reference_enable", True)
        else "off"
    )
    global_visual_plan: list[dict[str, object]] = []
    global_plan_cache_key = _global_visual_plan_cache_key(scenes_data, settings)
    cached_global_visual_plan = _load_cached_global_visual_plan(
        project_dir,
        scenes_data,
        pipeline_scenes,
        settings,
        log,
    )

    def capture_visual_plan(items: list[dict[str, object]]) -> None:
        global_visual_plan[:] = items
        atomic_write_text(
            project_dir / "03_global_visual_plan.json",
            json.dumps(global_visual_plan, ensure_ascii=False, indent=2),
        )
        atomic_write_text(
            project_dir / _GLOBAL_VISUAL_PLAN_CACHE,
            json.dumps(
                {
                    "schema_version": _GLOBAL_VISUAL_PLAN_CACHE_VERSION,
                    "cache_key": global_plan_cache_key,
                    "scene_count": len(pipeline_scenes),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        log(
            f"Глобальный визуальный план сохранён: "
            f"{project_dir / '03_global_visual_plan.json'}"
        )

    try:
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
            # video_prompt rules follow the configured image-to-video engine.
            video_engine=str(settings.get("video_engine") or "ltx"),
            on_visual_plan=capture_visual_plan,
            precomputed_global_visual_plan=cached_global_visual_plan,
            resume_shots=pipeline_resume_shots,
            on_progress=on_progress,
        )
    finally:
        _maybe_unload_lm(
            settings,
            "shot list generation finished",
            log,
            model=loaded_model_instance or model,
        )
    if not global_visual_plan:
        raise ValueError("LM Studio не вернула обязательный глобальный визуальный план")

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
    continuity.apply_storyboard_context(shots, global_visual_plan)

    # The LLM may suggest visual timing, but transcript segments remain the
    # immutable semantic authority. Persist their 1-based IDs so later engine
    # stages can prove coverage without re-interpreting prose.
    from core import production as production_contracts

    annotated = production_contracts.annotate_shot_segments(
        [shot.__dict__ for shot in shots],
        pipeline_segments,
    )
    for shot, item in zip(shots, annotated):
        shot.segment_ids = list(item["segment_ids"])
        shot.visual_concept_id = str(item["visual_concept_id"])
    anchored = production_contracts.enforce_shot_visual_anchors(
        shots,
        pipeline_segments,
    )
    if anchored:
        log(
            f"Фактические якоря: автоматически закреплены в {anchored} кадрах."
        )

    # Replace the per-scene/raw checkpoint with the fully scheduled and
    # annotated candidate before building secondary artifacts.  If a later
    # context/board step fails, diagnostics and recovery see the exact final
    # timeline rather than an earlier LM draft with apparent gaps.
    atomic_write_text(
        project_dir / "04_shot_list.partial.json",
        json.dumps(
            [shot.__dict__ for shot in shots],
            ensure_ascii=False,
            indent=2,
        ),
    )

    context_bible = continuity.build_context_bible(
        pipeline_scenes,
        global_visual_plan,
        shots,
        character_description=character_description,
    )
    context_path = continuity.write_context_bible(project_dir, context_bible)
    log(
        "Контекстная библия сохранена: "
        f"{context_path} · групп непрерывности "
        f"{len(context_bible['continuity_groups'])}"
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
                  "sfx_accent_query": getattr(s, "sfx_accent_query", ""),
                  "segment_ids": getattr(s, "segment_ids", []),
                  "visual_concept_id": getattr(s, "visual_concept_id", ""),
                  "purpose": getattr(s, "purpose", ""),
                  "continuity_mode": getattr(s, "continuity_mode", "independent"),
                  "continuity_group_id": getattr(s, "continuity_group_id", ""),
                  "location_id": getattr(s, "location_id", ""),
                  "subject_ids": getattr(s, "subject_ids", []),
                  "state_before": getattr(s, "state_before", ""),
                  "action": getattr(s, "action", ""),
                  "state_after": getattr(s, "state_after", ""),
                  "shot_size": getattr(s, "shot_size", ""),
                  "camera_angle": getattr(s, "camera_angle", ""),
                  "screen_direction": getattr(s, "screen_direction", ""),
                  "reference_frame_id": getattr(s, "reference_frame_id", ""),
                  "transition_in": getattr(s, "transition_in", ""),
                  "transition_out": getattr(s, "transition_out", ""),
                  "change_from_previous": getattr(s, "change_from_previous", "")}
                 for s in shots]

    return shots_out, _to_cards(cards)
