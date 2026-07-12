import csv
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    W,
    X,
    Y,
    BooleanVar,
    Button,
    Checkbutton,
    Entry,
    Frame,
    Label,
    Listbox,
    StringVar,
    Tk,
    filedialog,
    messagebox,
)
from tkinter.scrolledtext import ScrolledText
from tkinter import ttk

import schema_generator
from core.style_presets import (
    DEFAULT_VIDEO_STYLE,
    compose_image_prompt,
    compose_video_prompt,
    get_video_style,
    style_director_brief,
)
from core.export import tiled_intervals
from core.config import DEFAULT_SETTINGS_PATH, load_settings
from core.comfy import (
    comfy_base_url,
    comfy_collect_outputs,
    comfy_download_file,
    comfy_download_output,
    comfy_queue_prompt,
    comfy_request_json,
    comfy_upload_image,
    comfy_wait_for_history,
)
from core.comfy_generation import (
    COMFY_IDEOGRAM_WORKFLOW,
    COMFY_KREA_WORKFLOW,
    COMFY_LTX_WORKFLOW,
    DEFAULT_COMFY_ACE_WORKFLOW,
    DEFAULT_COMFY_IDEOGRAM_WORKFLOW,
    DEFAULT_COMFY_KREA_WORKFLOW,
    DEFAULT_COMFY_LTX_DRAFT_WORKFLOW,
    DEFAULT_COMFY_LTX_WORKFLOW,
    LTX_RESOLUTIONS,
    MAX_ACE_DURATION,
    MAX_LTX_DURATION,
    configured_workflow_path,
    generate_comfy_image,
    generate_comfy_ltx_video,
    generate_comfy_music,
    ltx_draft_for_card,
    ltx_render_resolution,
    prepare_image_workflow,
    prepare_ltx_workflow,
    prepare_music_workflow,
    project_video_style_id,
    set_workflow_seed_values,
    style_negative_for_clip,
)
from core.models import AssetCard, Scene, Shot, TranscriptSegment, legacy_asset_card_id
from core.transcribe import (
    find_default_model,
    find_default_tool,
    normalize_timestamp,
    parse_srt_segments,
    parse_whisper_json,
    run_process,
    transcribe_with_whisper_cpp,
    whisper_detect_language,
)
# Deterministic shot-list post-processing (route budgets, anti-slideshow,
# framing diversity) lives in core/shots.py — strangler slice #2, see
# docs/DEVELOPMENT.md. Re-imported here so existing call sites keep working.
from core.shots import (
    AI_MIX_RUNTIME_CAP,
    MAX_KENBURNS_RUN,
    ROUTE_AI_RUNTIME_CAP,
    diversify_repeated_shots,
    enforce_ai_route_budget,
    enforce_route_budget,
    split_shots_for_stock,
    split_stock_shots_only,
)


AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
SETTINGS_PATH = DEFAULT_SETTINGS_PATH
SHOT_TARGET_SECONDS = 4
# 15s cap: LTX cost per second grows super-linearly with clip length (attention
# over all frames + tiled VAE), and long i2v clips drift in quality. Shots longer
# than this are split by _split_long_shot; MAX_LTX_DURATION must stay in sync so
# a generated clip always covers its full timeline slot.
SHOT_MAX_SECONDS = 15

APP_BG = "#f3f6fb"
PANEL_BG = "#ffffff"
SIDEBAR_BG = "#101827"
SIDEBAR_FG = "#d8e2f0"
TEXT_FG = "#172033"
MUTED_FG = "#657083"
ACCENT = "#2f7cf6"
ACCENT_DARK = "#1d5fd0"
BORDER = "#d8e0ec"


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\wа-яё]+", "-", text, flags=re.IGNORECASE)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "content-project"


def clean_script_text(text: str) -> str:
    text = re.sub(r"\[(?:ПАУЗА(?: 1 СЕК)?|АКЦЕНТ|ТИШЕ|БЫСТРЕЕ|МЕДЛЕННЕЕ)\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^#.+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^-{3,}$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_into_blocks(text: str) -> list[str]:
    text = clean_script_text(text)
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    blocks: list[str] = []
    current: list[str] = []
    current_words = 0

    for paragraph in paragraphs:
        words = len(paragraph.split())
        if current and current_words + words > 95:
            blocks.append("\n\n".join(current))
            current = []
            current_words = 0
        current.append(paragraph)
        current_words += words

    if current:
        blocks.append("\n\n".join(current))

    return blocks


def seconds_to_timecode(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def seconds_to_srt_time(seconds: int) -> str:
    return seconds_to_timecode(seconds) + ",000"


def estimate_duration_seconds(text: str) -> int:
    words = len(text.split())
    return max(12, round(words / 2.25))


def make_title(text: str, index: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " ").strip())
    first = sentences[0] if sentences else f"Сцена {index}"
    first = re.sub(r"\s+", " ", first).strip()
    if len(first) > 58:
        first = first[:55].rsplit(" ", 1)[0] + "..."
    return first or f"Сцена {index}"


def make_screen_text(text: str) -> str:
    candidates = re.findall(r"[^.!?]+[.!?]?", text.replace("\n", " "))
    candidates = [re.sub(r"\s+", " ", item).strip(" .!?") for item in candidates if len(item.split()) >= 4]
    if not candidates:
        return ""
    candidate = max(candidates[:5], key=len)
    if len(candidate) > 54:
        candidate = candidate[:51].rsplit(" ", 1)[0] + "..."
    return candidate


def heuristic_visual_direction(text: str) -> str:
    lower = text.lower()
    rules = [
        (("спутник", "starlink", "космос"), "Спутники, орбита, затем резкий контраст с кабелями под океаном."),
        (("океан", "под водой", "дно", "глубин"), "Темный океан, карта маршрута через море, подводный кабель на дне."),
        (("телеграф", "виктория", "xix", "девятнадц"), "Архивные карты, телеграфные линии, старые гравюры и документы."),
        (("кабель", "волокн", "свет"), "Макро-схема кабеля, оптическое волокно, световые импульсы внутри."),
        (("кораб", "судно", "проклад"), "Кабелеукладочное судно, трюм с кабелем, медленное движение по морю."),
        (("якор", "трал", "рыболов", "рв"), "Якорь, рыболовный трал, аварийная точка на карте, тревожный B-roll."),
        (("bgp", "маршрут", "обход"), "Абстрактная сеть маршрутов, карта дорог, линии перестраиваются в обход аварии."),
        (("google", "meta", "microsoft", "amazon"), "Дата-центры, серверные ряды, логика Big Tech и глобальных магистралей."),
        (("бирж", "миллисекунд", "задерж"), "Финансовые графики, биржевые терминалы, гонка за миллисекундами."),
    ]
    for keywords, direction in rules:
        if any(keyword in lower for keyword in keywords):
            return direction
    return "Документальный B-roll по теме фразы, простая инфографика и крупная экранная подпись."


def heuristic_music_note(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("вопрос", "почему", "угроза", "опасн", "рв", "поврежд")):
        return "Низкий напряженный фон, короткая пауза перед ключевой фразой."
    if any(word in lower for word in ("история", "телеграф", "девятнадц", "1858")):
        return "Спокойная историческая подложка, мягкий темп."
    if any(word in lower for word in ("финал", "каждый раз", "прямо сейчас", "в виде света")):
        return "Широкая финальная подложка, медленное нарастание."
    return "Нейтральная документальная подложка, без перегруза."


def heuristic_retention_note(text: str) -> str:
    lower = text.lower()
    if "почему" in lower or "вопрос" in lower:
        return "Открытая петля: зритель ждет ответ."
    if any(word in lower for word in ("не спут", "миф", "правда", "на самом деле")):
        return "Разрушение мифа."
    if any(word in lower for word in ("самое странное", "неожидан", "парадокс")):
        return "Поворот ожиданий."
    return "Поддержать сменой визуала каждые 4-7 секунд."


def build_heuristic_scenes(script_text: str) -> list[Scene]:
    scenes: list[Scene] = []
    cursor = 0
    for index, block in enumerate(split_into_blocks(script_text), start=1):
        duration = estimate_duration_seconds(block)
        scene = Scene(
            index=index,
            start=seconds_to_timecode(cursor),
            end=seconds_to_timecode(cursor + duration),
            title=make_title(block, index),
            voice_text=block,
            visual_direction=heuristic_visual_direction(block),
            screen_text=make_screen_text(block),
            music_note=heuristic_music_note(block),
            retention_note=heuristic_retention_note(block),
        )
        scenes.append(scene)
        cursor += duration
    return scenes


def build_scenes_from_segments(segments: list[TranscriptSegment], target_seconds: int = 45) -> list[Scene]:
    scenes: list[Scene] = []
    current: list[TranscriptSegment] = []
    scene_start_seconds = 0

    for segment in segments:
        if not current:
            scene_start_seconds = timecode_to_seconds(segment.start)
        current.append(segment)
        duration = timecode_to_seconds(segment.end) - scene_start_seconds
        words = sum(len(item.text.split()) for item in current)
        if duration >= target_seconds or words >= 100:
            scenes.append(make_scene_from_segments(len(scenes) + 1, current))
            current = []

    if current:
        scenes.append(make_scene_from_segments(len(scenes) + 1, current))

    return scenes


def _lm_embeddings(base_url: str, model: str, texts: list[str], timeout: int = 120, ttl_seconds: int | None = None) -> list[list[float]]:
    """Fetch embeddings from an OpenAI-compatible endpoint (LM Studio /v1/embeddings).
    Raises on failure so the caller can fall back to a heuristic. `ttl_seconds`
    sets LM Studio's idle auto-unload TTL for the embedding model (JIT), same
    mechanism as the chat model."""
    endpoint = base_url.rstrip("/") + "/embeddings"
    payload = {"model": model, "input": texts}
    add_lm_ttl(payload, ttl_seconds)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
    embeddings = [item["embedding"] for item in items]
    if len(embeddings) != len(texts):
        raise ValueError(f"embedding count mismatch: {len(embeddings)} != {len(texts)}")
    return embeddings


def _cosine_sim(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _mean_vec(vecs: list[list[float]]) -> list[float]:
    n = len(vecs)
    dim = len(vecs[0])
    return [sum(v[d] for v in vecs) / n for d in range(dim)]


def _merge_segments_into_units(
    segments: list[TranscriptSegment], min_words: int = 12, max_unit_seconds: float = 14.0
) -> list[list[TranscriptSegment]]:
    """Group tiny whisper segments into utterance-sized units (sentence-ish) so
    embeddings are computed on meaningful chunks, not 2-3 second fragments."""
    units: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    words = 0
    unit_start = 0.0
    for seg in segments:
        if not current:
            unit_start = timecode_to_seconds(seg.start)
        current.append(seg)
        words += len((seg.text or "").split())
        dur = timecode_to_seconds(seg.end) - unit_start
        ends_sentence = (seg.text or "").rstrip().endswith((".", "!", "?", "…"))
        if (words >= min_words and ends_sentence) or dur >= max_unit_seconds:
            units.append(current)
            current = []
            words = 0
    if current:
        units.append(current)
    return units


def _block_similarity(embs: list[list[float]], gap: int, window: int) -> float:
    """TextTiling block comparison: cosine between the mean embedding of `window`
    units before the gap and `window` units after it."""
    left = embs[max(0, gap - window + 1): gap + 1]
    right = embs[gap + 1: gap + 1 + window]
    if not left or not right:
        return 1.0
    return _cosine_sim(_mean_vec(left), _mean_vec(right))


def _depth_scores(gap_scores: list[float]) -> list[float]:
    """For each gap, how deep its similarity valley is vs the nearest peaks on
    both sides. High depth = strong topic boundary."""
    n = len(gap_scores)
    depths = [0.0] * n
    for i in range(n):
        l = i
        while l > 0 and gap_scores[l - 1] >= gap_scores[l]:
            l -= 1
        r = i
        while r < n - 1 and gap_scores[r + 1] >= gap_scores[r]:
            r += 1
        depths[i] = (gap_scores[l] - gap_scores[i]) + (gap_scores[r] - gap_scores[i])
    return depths


def build_scenes_by_embedding(
    segments: list[TranscriptSegment],
    base_url: str,
    model: str = "text-embedding-nomic-embed-text-v1.5",
    min_seconds: int = 20,
    max_seconds: int = 90,
    sensitivity: float = 0.6,
    fallback: bool = True,
    ttl_seconds: int | None = None,
    target_scenes: int = 0,
    window: int = 2,
    min_unit_words: int = 12,
    log_callback=None,
) -> list[Scene]:
    """Deterministic topic segmentation (windowed TextTiling):

    1. Merge tiny whisper segments into utterance-sized units (cleaner embeddings).
    2. Embed units; for each gap compute block similarity over a window of units.
    3. Compute depth scores (valley depth) and cut where depth exceeds a cutoff
       (sensitivity-controlled) — or pick the top boundaries to hit `target_scenes`.
    4. Enforce min/max scene duration (merge too-short, split too-long at the
       strongest internal gap).

    If embeddings are unavailable: `fallback=True` → duration heuristic; else re-raise.
    """
    if not segments:
        return []

    units = _merge_segments_into_units(segments, min_words=min_unit_words)
    unit_texts = [" ".join((s.text or "").strip() for s in u).strip() or " " for u in units]
    try:
        embs = _lm_embeddings(base_url, model, unit_texts, ttl_seconds=ttl_seconds)
    except Exception:
        if fallback:
            return build_scenes_from_segments(segments)
        raise

    if len(units) <= 1:
        flat = [s for u in units for s in u] or segments
        return [make_scene_from_segments(1, flat)]

    n_gaps = len(units) - 1
    w = max(1, int(window))
    gaps = [_block_similarity(embs, i, w) for i in range(n_gaps)]
    depths = _depth_scores(gaps)

    import statistics
    if target_scenes and target_scenes > 1:
        order = sorted(range(n_gaps), key=lambda i: depths[i], reverse=True)
        chosen = set(order[: max(0, int(target_scenes) - 1)])
    else:
        mean_d = statistics.fmean(depths) if depths else 0.0
        std_d = statistics.pstdev(depths) if len(depths) > 1 else 0.0
        # Higher sensitivity -> lower cutoff -> more boundaries.
        cutoff = mean_d + (0.5 - sensitivity) * std_d
        chosen = {i for i in range(n_gaps) if depths[i] > 0 and depths[i] >= cutoff}

    # Build unit-groups split at chosen gaps.
    groups: list[list[int]] = []
    start = 0
    for i in range(n_gaps):
        if i in chosen:
            groups.append(list(range(start, i + 1)))
            start = i + 1
    groups.append(list(range(start, len(units))))

    def gdur(g: list[int]) -> float:
        return timecode_to_seconds(units[g[-1]][-1].end) - timecode_to_seconds(units[g[0]][0].start)

    # MERGE: scenes shorter than min_seconds fold into a neighbour.
    changed = True
    while changed and len(groups) > 1:
        changed = False
        for idx, g in enumerate(groups):
            if gdur(g) < min_seconds:
                if idx + 1 < len(groups):
                    groups[idx + 1] = g + groups[idx + 1]
                else:
                    groups[idx - 1] = groups[idx - 1] + g
                groups.pop(idx)
                changed = True
                break

    # SPLIT: scenes longer than max_seconds split at the strongest internal gap.
    def split_long(group: list[int]) -> list[list[int]]:
        if gdur(group) <= max_seconds or len(group) < 2:
            return [group]
        cut = max(group[:-1], key=lambda u: depths[u])
        left = [u for u in group if u <= cut]
        right = [u for u in group if u > cut]
        return split_long(left) + split_long(right)

    final: list[list[int]] = []
    for g in groups:
        final.extend(split_long(g))

    if log_callback:
        log_callback(
            f"TextTiling: реплик={len(units)}, границ={len(final) - 1}, сцен={len(final)} "
            f"(sensitivity={sensitivity}, target={target_scenes or 'auto'}, окно={w})"
        )

    scenes: list[Scene] = []
    for gi, g in enumerate(final, start=1):
        segs = [s for u in g for s in units[u]]
        scenes.append(make_scene_from_segments(gi, segs))
    return scenes


def enrich_scenes_with_lm(
    scenes: list[Scene],
    base_url: str,
    model: str,
    ttl_seconds: int | None = None,
    style_anchor: str = "",
    character_description: str = "",
    niche: str = "documentary",
    video_style: str = DEFAULT_VIDEO_STYLE,
) -> list[Scene]:
    """Fill creative fields (title, visual_direction, screen_text, music_note,
    retention_note) for scenes whose boundaries are ALREADY fixed (e.g. from
    embedding segmentation). The LLM only LABELS each scene by index — it must
    not merge or split. Raises on LM failure so the caller can keep the
    heuristic labels the scenes already carry."""
    if not scenes:
        return scenes
    style = get_video_style(video_style)
    style_block = "\n" + style_director_brief(video_style, style_anchor) + "\n"
    character_block = f"ПЕРСОНАЖ (одинаков во всех сценах): {character_description}\n" if character_description else ""
    compact = "\n".join(
        f"{s.index}|{s.start}-{s.end}|{(s.voice_text or '').strip()}" for s in scenes
    )
    prompt = (
        f"Ты режиссёр монтажа YouTube-контента в стиле \"{style.name}\".\n\n"
        "Границы сцен УЖЕ заданы. НЕ объединяй и НЕ разбивай их, НЕ меняй index/тайминги/текст.\n"
        "Для КАЖДОЙ сцены по её тексту придумай поля:\n"
        "- title — короткое название сцены;\n"
        "- visual_direction — КОНКРЕТНОЕ описание сцены в выбранном стиле: локация, кто/что в кадре, ракурс, "
        "эмоция, простой визуальный символ; не копируй текст диктора дословно;\n"
        "- screen_text — короткая надпись на экран (2-7 слов) или пусто;\n"
        "- music_note — короткая заметка по музыке;\n"
        "- retention_note — заметка по удержанию.\n"
        f"{style_block}{character_block}"
        "title и screen_text — на ТОМ ЖЕ ЯЗЫКЕ, что и текст сцены (voice_text); не переводи их на русский.\n"
        "visual_direction делай разным для соседних сцен (чередуй планы).\n"
        "Верни ТОЛЬКО валидный JSON-массив объектов с полями: "
        "index, title, visual_direction, screen_text, music_note, retention_note.\n"
        "index — номер сцены из входа.\n\n"
        "Сцены (index|start-end|текст):\n"
        f"{compact}"
    )
    payload = {
        "model": model.strip() or "local-model",
        "messages": [
            {"role": "system", "content": "Отвечай только валидным JSON. Не добавляй markdown. visual_direction — буквальная физическая сцена, без метафор и символов."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }
    add_lm_ttl(payload, ttl_seconds)
    endpoint = base_url.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"].strip()
    raw = parse_lm_json_array(content, "LM scene enrichment")
    by_index: dict[int, dict] = {}
    for item in raw:
        try:
            by_index[int(item.get("index"))] = item
        except (TypeError, ValueError):
            continue
    for scene in scenes:
        item = by_index.get(scene.index)
        if not item:
            continue
        scene.title = str(item.get("title", scene.title)).strip() or scene.title
        scene.visual_direction = str(item.get("visual_direction", scene.visual_direction)).strip() or scene.visual_direction
        scene.screen_text = str(item.get("screen_text", scene.screen_text)).strip()
        scene.music_note = str(item.get("music_note", scene.music_note)).strip() or scene.music_note
        scene.retention_note = str(item.get("retention_note", scene.retention_note)).strip() or scene.retention_note
    return scenes


def build_heuristic_shots(
    scenes: list[Scene],
    image_preference: str = "Auto",
    style_anchor: str = "",
    video_style: str = DEFAULT_VIDEO_STYLE,
    character_description: str = "",
    compose: bool = True,
) -> list[Shot]:
    shots: list[Shot] = []
    for scene in scenes:
        scene_start = timecode_to_seconds(scene.start)
        scene_end = max(scene_start + 1, timecode_to_seconds(scene.end))
        duration = scene_end - scene_start
        shot_count = max(1, round(duration / SHOT_TARGET_SECONDS))
        step = duration / shot_count
        for local_index in range(shot_count):
            start_seconds = round(scene_start + local_index * step)
            end_seconds = round(scene_start + (local_index + 1) * step)
            if local_index == shot_count - 1:
                end_seconds = scene_end
            text_hint = scene.voice_text if local_index == 0 else scene.visual_direction
            route = choose_visual_route(
                text_hint,
                scene.screen_text,
                image_preference=image_preference,
                style_anchor=style_anchor,
                video_style=video_style,
                character_description=character_description,
            )
            description = make_shot_description(scene, local_index, shot_count)
            shots.append(
                Shot(
                    index=len(shots) + 1,
                    scene_index=scene.index,
                    start=seconds_to_timecode(start_seconds),
                    end=seconds_to_timecode(end_seconds),
                    visual_type=route["visual_type"],
                    description=description,
                    asset_query=route["asset_query"],
                    image_model=route["image_model"],
                    video_model=route["video_model"],
                    image_prompt=route["image_prompt"],
                    video_prompt=route["video_prompt"],
                    screen_text=scene.screen_text if local_index == 0 else "",
                    motion=route["motion"],
                    priority="high" if local_index == 0 else "medium",
                    fallback=route["fallback"],
                )
            )
    repair_shot_prompts(shots)
    # Apply the visual style exactly once, over the neutral bodies. Callers that
    # run their own compose pass afterwards (e.g. the LLM shot-list path) pass
    # compose=False to avoid double-applying the style.
    if compose:
        for s in shots:
            s.image_prompt = compose_image_prompt(
                video_style,
                s.image_prompt or s.description,
                character_description=character_description,
                custom_notes=style_anchor,
            )
            s.video_prompt = compose_video_prompt(
                video_style,
                s.video_prompt or s.motion,
                shot_description=s.description,
            )
    return shots


def choose_visual_route(
    text: str,
    screen_text: str = "",
    image_preference: str = "Auto",
    style_anchor: str = "",
    video_style: str = DEFAULT_VIDEO_STYLE,
    character_description: str = "",
) -> dict:
    lower = f"{text} {screen_text}".lower()
    preferred_image_model = preferred_realistic_image_model(image_preference)
    # Return NEUTRAL bodies only. build_heuristic_shots applies the style once via
    # compose_* at the end, so we do NOT compose here (that would double-apply the
    # style and bury the subject).
    body = make_realistic_prompt(text)
    video_body = make_ltx_prompt(body)
    return {
        "visual_type": "ai_image_then_i2v",
        "asset_query": make_asset_query(lower),
        "image_model": preferred_image_model,
        "video_model": "ltx2.3_i2v",
        "image_prompt": body,
        "video_prompt": video_body,
        "motion": "slow documentary camera movement",
        "fallback": "AI image-to-video generation",
    }


def preferred_realistic_image_model(image_preference: str) -> str:
    preference = image_preference.strip().lower()
    if preference == "ideogram":
        return "ideogram4"
    return "krea2_turbo"


def make_asset_query(text: str) -> str:
    keywords = []
    for key, label in [
        ("океан", "deep ocean"),
        ("кабель", "submarine cable"),
        ("дата", "data center"),
        ("сервер", "server room"),
        ("кораб", "cable laying ship"),
        ("судно", "cable laying ship"),
        ("спут", "satellite orbit"),
        ("бирж", "stock exchange"),
        ("телеграф", "telegraph archive"),
    ]:
        if key in text:
            keywords.append(label)
    return " ".join(dict.fromkeys(keywords)) or "documentary technology broll"


def make_realistic_prompt(text: str, shot_type: str = "", style_anchor: str = "") -> str:
    # Build a NEUTRAL, concrete scene BODY only — a framing + the real scene.
    # No "photorealistic", no style words: the visual style is applied exactly
    # once, later, by compose_image_prompt. Baking realism/style here caused it
    # to fight the selected style (e.g. photoreal body under a knit header).
    # `style_anchor` is accepted for backward compatibility but intentionally
    # not injected — style is a single responsibility of compose_image_prompt.
    hint = clean_visual_prompt_source(text)
    if len(hint) > 300:
        hint = hint[:297].rsplit(" ", 1)[0] + "..."
    shot = shot_type or _suggest_shot_type(hint)
    return (
        f"{shot} of {hint}, a real recognizable place shown with clear shapes, "
        f"colours and its own landmarks"
    )


def _suggest_shot_type(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ("macro", "detail", "волокн", "cable", "close", "microscop")):
        return "extreme macro detail shot"
    if any(w in lower for w in ("aerial", "вид сверху", "overview", "data center", "city", "город")):
        return "wide aerial establishing shot"
    if any(w in lower for w in ("портрет", "face", "portrait", "person", "человек", "инженер", "учён")):
        return "medium close-up portrait with environmental context"
    if any(w in lower for w in ("underwater", "океан", "под водой", "ocean floor", "море", "дно")):
        return "underwater wide shot"
    if any(w in lower for w in ("ship", "судно", "кораб", "vessel", "лодк")):
        return "dramatic low angle wide shot"
    if any(w in lower for w in ("building", "здани", "architecture", "архитектур", "factory", "завод")):
        return "low angle architectural shot"
    if any(w in lower for w in ("server", "сервер", "data center", "datacenter")):
        return "symmetrical wide shot of server rows"
    if any(w in lower for w in ("hand", "рука", "палец", "fingers", "инструмент", "tool")):
        return "extreme close-up of hands at work"
    if any(w in lower for w in ("историческ", "archive", "архив", "старин", "old", "antique", "винтаж")):
        return "vintage archival still"
    if any(w in lower for w in ("terminal", "screen", "бирж", "stock")):
        return "medium shot of glowing screens"
    return "medium establishing shot"


def make_ltx_prompt(image_prompt: str, motion_hint: str = "") -> str:
    motion = motion_hint or _suggest_motion(image_prompt)
    return (
        f"The camera {motion}. "
        "Subtle handmade movement brings the shot to life while the composition stays stable. "
        "Small environmental motion is visible where appropriate: light shifts, particles drift, fabric or water moves softly."
    )


def apply_style_anchor(prompt: str, style_anchor: str) -> str:
    """Force the project's visual style to lead the image prompt. Image models
    weight the first tokens most, so prepending the style anchor reliably applies
    it even when the LLM body drifts toward realism. No-op if already present."""
    s = (style_anchor or "").strip()
    if not s or not prompt:
        return prompt
    if s.lower() in prompt.lower():
        return prompt
    return f"{s}, {prompt}"


def _suggest_motion(text: str) -> str:
    # Honest, FORM-BASED default only. We do NOT guess a subject's action from
    # keywords (that invents motion that may contradict the image). We only pick a
    # camera behaviour that fits the shot's framing; ambient motion is added by the
    # caller and the subject is kept still. Used strictly as a last-resort fallback
    # when the model gave no usable motion.
    lower = text.lower()
    if any(w in lower for w in ("wide", "aerial", "establishing", "landscape", "overview", "panorama", "vista")):
        return "slowly drifts across the scene, letting the space and its landmarks read"
    if any(w in lower for w in ("close-up", "close up", "macro", "extreme", "detail")):
        return "gently pushes in on the detail and holds, keeping the subject stable"
    return "slowly pushes in and holds steady, letting the scene settle"


def _motion_is_weak(shot) -> bool:
    """A shot's motion is 'weak' when the model gave nothing usable: empty or a
    couple of words. Used only to decide whether to retry ONCE for better motion —
    it does NOT judge 'too little movement' (a still scene is a valid answer)."""
    m = ((getattr(shot, "video_prompt", "") or "") + " " + (getattr(shot, "motion", "") or "")).strip()
    if not m:
        return True
    return len(m.split()) < 4


def _motions_mostly_weak(shots) -> bool:
    if not shots:
        return False
    weak = sum(1 for s in shots if _motion_is_weak(s))
    return weak > len(shots) / 2


def clean_visual_prompt_source(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = cleaned.replace("->", ", then ")
    cleaned = re.sub(r"\bSupporting B-roll or graphic for scene:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bClosing visual for scene:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bScene idea:\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" .,:;") or "documentary technology visual"


def prompt_has_too_much_transcript(prompt: str) -> bool:
    if not prompt.strip():
        return True
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", prompt))
    letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", prompt))
    if letters and cyrillic / letters > 0.18:
        return True
    transcript_markers = [
        "вот вы отправили",
        "если спросить",
        "в этот момент ваши данные",
        "как образом",
        "каким образом",
        "по дну океана",
    ]
    return any(marker in prompt.lower() for marker in transcript_markers)


def rebuild_image_prompt_for_shot(shot: Shot) -> str:
    # Emergency rebuild when the model's prompt is unusable (too much raw
    # transcript). Returns a NEUTRAL concrete BODY only — no "photorealistic",
    # no forced style — so that compose_image_prompt can apply the selected style
    # exactly once on top. (Typography cards are the one intentional exception.)
    source = shot.description or shot.asset_query or shot.screen_text or "a simple real scene"
    source = clean_visual_prompt_source(source)
    if shot.asset_query and shot.asset_query.lower() not in source.lower():
        source = f"{source}, {shot.asset_query}"
    shot_type = _suggest_shot_type(source)
    if shot.visual_type == "ai_typography_card":
        return (
            "Clean cinematic editorial title card, strong composition, "
            "high contrast, 16:9 layout, minimal visual clutter. "
            f"Visible text, exactly: \"{shot.screen_text}\". Visual idea: {source}"
        )
    return (
        f"{shot_type} of {source}, a real recognizable place shown with clear "
        f"shapes, colours and its own landmarks"
    )


def repair_shot_prompts(shots: list[Shot]):
    for shot in shots:
        if shot.image_model and prompt_has_too_much_transcript(shot.image_prompt):
            shot.image_prompt = rebuild_image_prompt_for_shot(shot)
        if shot.video_model and prompt_has_too_much_transcript(shot.video_prompt):
            shot.video_prompt = make_ltx_prompt(shot.image_prompt)


def make_shot_description(scene: Scene, local_index: int, shot_count: int) -> str:
    if local_index == 0:
        return scene.visual_direction or scene.title
    if local_index == shot_count - 1:
        return f"Closing visual for scene: {scene.title}"
    return f"Supporting B-roll or graphic for scene: {scene.title}"


def make_scene_from_segments(index: int, segments: list[TranscriptSegment]) -> Scene:
    text = " ".join(segment.text.strip() for segment in segments).strip()
    return Scene(
        index=index,
        start=segments[0].start,
        end=segments[-1].end,
        title=make_title(text, index),
        voice_text=text,
        visual_direction=heuristic_visual_direction(text),
        screen_text=make_screen_text(text),
        music_note=heuristic_music_note(text),
        retention_note=heuristic_retention_note(text),
    )


def add_lm_ttl(payload: dict, ttl_seconds: int | None):
    if ttl_seconds is not None and ttl_seconds > 0:
        payload["ttl"] = ttl_seconds



def clean_lm_json_content(content: str) -> str:
    content = (content or "").strip()
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    content = re.sub(r':\s*\[([^\]]+)\]"\s*([,\}\]])', r': [\1]\2', content)
    return content.strip()


def parse_lm_json_array(content: str, label: str = "LM Studio") -> list:
    content = clean_lm_json_content(content)
    start = content.find("[")
    if start < 0:
        raise ValueError(f"{label} не вернул JSON-массив. Попробуйте повторить запрос или уменьшить длину транскрипции.")
    candidate = content[start:]

    decoder = json.JSONDecoder()
    decoder_lenient = json.JSONDecoder(strict=False)

    def _try_decode(text: str):
        for d in (decoder, decoder_lenient):
            try:
                return d.raw_decode(text)
            except json.JSONDecodeError:
                pass
            try:
                parsed = json.loads(text)
                return parsed, len(text)
            except json.JSONDecodeError:
                pass
            try:
                parsed = json.loads(text, strict=False)
                return parsed, len(text)
            except json.JSONDecodeError:
                pass
        return None

    result = _try_decode(candidate)
    if result is not None:
        parsed, _ = result
        if isinstance(parsed, list):
            return parsed

    end = candidate.rfind("]")
    if end > 0:
        trimmed = candidate[:end+1]
        result = _try_decode(trimmed)
        if result is not None:
            parsed, _ = result
            if isinstance(parsed, list):
                return parsed

    try:
        fixed = candidate.rstrip()
        if fixed[-1] != "]":
            last_obj_end = fixed.rfind("}")
            if last_obj_end > 0:
                fixed = fixed[:last_obj_end+1] + "]"
        result = _try_decode(fixed)
        if result is not None:
            parsed, _ = result
            if isinstance(parsed, list):
                return parsed
    except (json.JSONDecodeError, IndexError):
        pass

    debug_len = len(candidate)
    debug_head = candidate[:300]
    debug_tail = candidate[-300:]
    raise ValueError(
        f"{label} вернул поврежденный JSON (len={debug_len}). "
        f"HEAD: {debug_head!r} ... TAIL: {debug_tail!r}. "
        "Повторите анализ; если повторяется, уменьшите длину транскрипции или выберите более стабильную модель."
    )

def call_lm_studio(
    base_url: str,
    model: str,
    script_text: str,
    ttl_seconds: int | None = None,
    style_anchor: str = "",
    character_description: str = "",
    niche: str = "documentary",
    video_style: str = DEFAULT_VIDEO_STYLE,
) -> list[Scene]:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    selected_style = get_video_style(video_style)
    style_block = "\n" + style_director_brief(video_style, style_anchor) + "\n"
    character_block = (
        f"ПЕРСОНАЖ (должен быть одинаков во всех сценах): {character_description}\n"
        if character_description else ""
    )
    prompt = (
        f"Ты режиссёр монтажа YouTube-контента в стиле \"{selected_style.name}\".\n\n"
        f"Разбей русский дикторский текст на сцены для монтажа.\n"
        f"Для КАЖДОЙ сцены РАЗВЕРНИ текст в визуальную сцену:\n"
        f"- Какую локацию, время суток, освещение мы видим?\n"
        f"- Кто или что в кадре? (персонаж, объект, техника, природа)\n"
        f"- Какое действие происходит?\n"
        f"- С какого ракурса снят кадр? (общий план, средний, деталь, сверху)\n"
        f"{style_block}"
        f"{character_block}"
        f"Правила:\n"
        f"- visual_direction должен быть КОНКРЕТНЫМ: не просто 'Офис', а "
        f"'Open-plan office, late afternoon, warm sunlight through venetian blinds, medium shot'\n"
        f"- Не повторяй один и тот же тип кадра/локацию в соседних сценах\n"
        f"- Чередуй: если сцена 1 — интерьер/крупный план, то сцена 2 — экстерьер/общий план\n"
        f"Верни только JSON-массив без markdown. Каждый объект: "
        "title, voice_text, visual_direction, screen_text, music_note, retention_note. "
        "voice_text должен быть точным фрагментом исходного текста, без выдуманных фактов.\n\n"
        f"Текст:\n{clean_script_text(script_text)}"
    )
    payload = {
        "model": model.strip() or "local-model",
        "messages": [
            {"role": "system", "content": "Отвечай только валидным JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }
    add_lm_ttl(payload, ttl_seconds)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        data = json.loads(response.read().decode("utf-8"))

    content = data["choices"][0]["message"]["content"].strip()
    raw_scenes = parse_lm_json_array(content, "LM Studio scene analysis")

    scenes: list[Scene] = []
    cursor = 0
    for index, item in enumerate(raw_scenes, start=1):
        voice_text = str(item.get("voice_text", "")).strip()
        duration = estimate_duration_seconds(voice_text)
        scenes.append(
            Scene(
                index=index,
                start=seconds_to_timecode(cursor),
                end=seconds_to_timecode(cursor + duration),
                title=str(item.get("title", f"Сцена {index}")).strip(),
                voice_text=voice_text,
                visual_direction=str(item.get("visual_direction", "")).strip(),
                screen_text=str(item.get("screen_text", "")).strip(),
                music_note=str(item.get("music_note", "")).strip(),
                retention_note=str(item.get("retention_note", "")).strip(),
            )
        )
        cursor += duration
    return scenes


def call_lm_studio_for_segments(
    base_url: str,
    model: str,
    segments: list[TranscriptSegment],
    ttl_seconds: int | None = None,
    style_anchor: str = "",
    character_description: str = "",
    niche: str = "documentary",
    video_style: str = DEFAULT_VIDEO_STYLE,
) -> list[Scene]:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    compact_transcript = "\n".join(
        f"{index}|{segment.start}-{segment.end}|{segment.text.strip()}"
        for index, segment in enumerate(segments, start=1)
    )
    selected_style = get_video_style(video_style)
    style_block = "\n" + style_director_brief(video_style, style_anchor) + "\n"
    character_block = (
        f"ПЕРСОНАЖ (должен выглядеть одинаково во всех сценах): {character_description}\n"
        if character_description else ""
    )
    prompt = (
        f"Ты режиссёр монтажа YouTube-контента в стиле \"{selected_style.name}\".\n\n"
        "У тебя есть точная транскрибация с таймкодами всего ролика. "
        "Сгруппируй соседние сегменты в смысловые сцены для монтажа.\n\n"
        "Для КАЖДОЙ сцены РАЗВЕРНИ текст в визуальную сцену:\n"
        "- Какую локацию, время суток, освещение мы видим?\n"
        "- Кто или что в кадре? (персонаж, объект, техника, природа)\n"
        "- Какое действие происходит?\n"
        "- С какого ракурса снят кадр? (общий план, средний, деталь, сверху, от первого лица)\n"
        f"{style_block}"
        f"{character_block}"
        "ВАЖНО: visual_direction должен быть КОНКРЕТНЫМ и переведенным в выбранный стиль. "
        "Если текст абстрактный, придумай простой визуальный символ внутри стиля.\n\n"
        "Правила:\n"
        "1. Используй только соседние segment id, не меняй порядок.\n"
        "2. start сцены должен быть start первого сегмента, end сцены - end последнего сегмента.\n"
        "3. Не выдумывай новый дикторский текст и факты.\n"
        "4. Верни только валидный JSON-массив без markdown.\n"
        "5. Каждый объект: title, segment_ids, purpose, visual_direction, screen_text, music_note, retention_note.\n"
        "6. segment_ids должны быть массивом чисел, например [1,2,3,4].\n"
        "7. Не возвращай voice_text и не переписывай транскрибацию.\n"
        "8. Сцена = одна законченная подтема / процесс / аргумент.\n"
        "   Если диктор говорит об одном и том же 2 минуты — это ОДНА сцена, не 4.\n"
        "   Новая сцена = смена подтемы, поворот в повествовании, новый пример/персонаж/место.\n"
        "   Минимум ~20 секунд. Максимум не ограничен — пока смысл не сменился.\n"
        "9. ВАЖНО: Если несколько соседних сегментов об одной теме (технология, процесс, персонаж, место, "
        "событие) — объединяй их в ОДНУ сцену. Не разбивай логически единую тему на 2-3 мелкие сцены.\n"
        "10. title и visual_direction должны быть РАЗНЫМИ для каждой сцены — уникальный визуальный "
        "ракурс и ключевая мысль. Хорошее разнообразие: сцена 1 — интерьер/крупный план, "
        "сцена 2 — экстерьер/общий план, сцена 3 — деталь/макро.\n"
        "11. Для каждого сегмента придумывай конкретный visual_direction с описанием типа кадра.\n\n"
        "Формат транскрибации: id|start-end|text\n\n"
        f"{compact_transcript}"
    )
    payload = {
        "model": model.strip() or "local-model",
        "messages": [
            {"role": "system", "content": "Отвечай только валидным JSON. Не добавляй markdown."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
    }
    add_lm_ttl(payload, ttl_seconds)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        data = json.loads(response.read().decode("utf-8"))

    content = data["choices"][0]["message"]["content"].strip()
    raw_scenes = parse_lm_json_array(content, "LM Studio segment analysis")
    by_id = {index: segment for index, segment in enumerate(segments, start=1)}
    scenes: list[Scene] = []
    last_id = 0

    for raw in raw_scenes:
        ids = raw.get("segment_ids") or []
        ids = [int(item) for item in ids if int(item) in by_id]
        ids = sorted(set(ids))
        if not ids:
            continue
        if ids[0] <= last_id:
            ids = [item for item in ids if item > last_id]
        if not ids:
            continue
        selected = [by_id[item] for item in ids]
        text = " ".join(segment.text.strip() for segment in selected).strip()
        scenes.append(
            Scene(
                index=len(scenes) + 1,
                start=selected[0].start,
                end=selected[-1].end,
                title=str(raw.get("title", f"Сцена {len(scenes) + 1}")).strip(),
                voice_text=text,
                visual_direction=str(raw.get("visual_direction", "")).strip(),
                screen_text=str(raw.get("screen_text", "")).strip(),
                music_note=str(raw.get("music_note", "")).strip(),
                retention_note=str(raw.get("retention_note", raw.get("purpose", ""))).strip(),
            )
        )
        last_id = ids[-1]

    if last_id < len(segments):
        scenes.extend(build_scenes_from_segments(segments[last_id:]))
        for index, scene in enumerate(scenes, start=1):
            scene.index = index

    scenes = _merge_similar_scenes(scenes)
    return scenes


def stream_lm_studio_segments(
    base_url: str,
    model: str,
    segments: list[TranscriptSegment],
    on_scene: Callable[[Scene], None] | None = None,
    ttl_seconds: int | None = None,
    style_anchor: str = "",
    character_description: str = "",
    niche: str = "documentary",
    video_style: str = DEFAULT_VIDEO_STYLE,
) -> list[Scene]:
    """Same as call_lm_studio_for_segments but streams scenes via on_scene callback
    as they arrive from LM Studio (server-sent events)."""
    endpoint = base_url.rstrip("/") + "/chat/completions"
    compact_transcript = "\n".join(
        f"{index}|{segment.start}-{segment.end}|{segment.text.strip()}"
        for index, segment in enumerate(segments, start=1)
    )
    selected_style = get_video_style(video_style)
    style_block = "\n" + style_director_brief(video_style, style_anchor) + "\n"
    character_block = (
        f"ПЕРСОНАЖ (должен выглядеть одинаково во всех сценах): {character_description}\n"
        if character_description else ""
    )
    prompt = (
        f"Ты режиссёр монтажа YouTube-контента в стиле \"{selected_style.name}\".\n\n"
        "У тебя есть точная транскрибация с таймкодами всего ролика. "
        "Сгруппируй соседние сегменты в смысловые сцены для монтажа.\n\n"
        "Для КАЖДОЙ сцены РАЗВЕРНИ текст в визуальную сцену:\n"
        "- Какую локацию, время суток, освещение мы видим?\n"
        "- Кто или что в кадре? (персонаж, объект, техника, природа)\n"
        "- Какое действие происходит?\n"
        "- С какого ракурса снят кадр? (общий план, средний, деталь, сверху, от первого лица)\n"
        f"{style_block}"
        f"{character_block}"
        "ВАЖНО: visual_direction должен быть КОНКРЕТНЫМ и переведенным в выбранный стиль. "
        "Если текст абстрактный, придумай простой визуальный символ внутри стиля.\n\n"
        "Правила:\n"
        "1. Используй только соседние segment id, не меняй порядок.\n"
        "2. start сцены должен быть start первого сегмента, end сцены - end последнего сегмента.\n"
        "3. Не выдумывай новый дикторский текст и факты.\n"
        "4. Верни только валидный JSON-массив без markdown.\n"
        "5. Каждый объект: title, segment_ids, purpose, visual_direction, screen_text, music_note, retention_note.\n"
        "6. segment_ids должны быть массивом чисел, например [1,2,3,4].\n"
        "7. Не возвращай voice_text и не переписывай транскрибацию.\n"
        "8. Сцена = одна законченная подтема / процесс / аргумент.\n"
        "   Если диктор говорит об одном и том же 2 минуты — это ОДНА сцена, не 4.\n"
        "   Новая сцена = смена подтемы, поворот в повествовании, новый пример/персонаж/место.\n"
        "   Минимум ~20 секунд. Максимум не ограничен — пока смысл не сменился.\n"
        "9. ВАЖНО: Если несколько соседних сегментов об одной теме (технология, процесс, персонаж, место, "
        "событие) — объединяй их в ОДНУ сцену. Не разбивай логически единую тему на 2-3 мелкие сцены.\n"
        "10. title и visual_direction должны быть РАЗНЫМИ для каждой сцены — уникальный визуальный "
        "ракурс и ключевая мысль. Хорошее разнообразие: сцена 1 — интерьер/крупный план, "
        "сцена 2 — экстерьер/общий план, сцена 3 — деталь/макро.\n"
        "11. Для каждого сегмента придумывай конкретный visual_direction с описанием типа кадра.\n\n"
        "Формат транскрибации: id|start-end|text\n\n"
        f"{compact_transcript}"
    )
    payload = {
        "model": model.strip() or "local-model",
        "messages": [
            {"role": "system", "content": "Отвечай только валидным JSON. Не добавляй markdown."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "stream": True,
    }
    add_lm_ttl(payload, ttl_seconds)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )

    by_id = {index: segment for index, segment in enumerate(segments, start=1)}
    raw_scenes: list[dict] = []
    scenes: list[Scene] = []
    last_id = 0
    buf = ""
    depth = 0
    in_object = False

    def _flush_object(obj_str: str):
        nonlocal last_id
        try:
            raw = json.loads(obj_str)
        except json.JSONDecodeError:
            return
        raw_scenes.append(raw)
        ids = raw.get("segment_ids") or []
        ids = [int(item) for item in ids if int(item) in by_id]
        ids = sorted(set(ids))
        if not ids:
            return
        if ids[0] <= last_id:
            ids = [item for item in ids if item > last_id]
        if not ids:
            return
        selected = [by_id[item] for item in ids]
        text = " ".join(segment.text.strip() for segment in selected).strip()
        scene = Scene(
            index=len(scenes) + 1,
            start=selected[0].start,
            end=selected[-1].end,
            title=str(raw.get("title", f"Сцена {len(scenes) + 1}")).strip(),
            voice_text=text,
            visual_direction=str(raw.get("visual_direction", "")).strip(),
            screen_text=str(raw.get("screen_text", "")).strip(),
            music_note=str(raw.get("music_note", "")).strip(),
            retention_note=str(raw.get("retention_note", raw.get("purpose", ""))).strip(),
        )
        scenes.append(scene)
        last_id = ids[-1]
        if on_scene:
            on_scene(scene)

    with urllib.request.urlopen(request, timeout=300) as response:
        for line in response:
            raw_line = line.decode("utf-8").strip()
            if not raw_line.startswith("data: "):
                continue
            data = raw_line[6:]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                token = chunk["choices"][0]["delta"].get("content", "")
            except (json.JSONDecodeError, KeyError, IndexError):
                continue

            for ch in token:
                if ch == '{':
                    if not in_object:
                        in_object = True
                        buf = '{'
                        depth = 1
                    else:
                        buf += ch
                        depth += 1
                elif ch == '}':
                    buf += ch
                    depth -= 1
                    if depth == 0 and in_object:
                        _flush_object(buf)
                        in_object = False
                        buf = ""
                elif in_object:
                    buf += ch

    if last_id < len(segments):
        scenes.extend(build_scenes_from_segments(segments[last_id:]))
        for index, scene in enumerate(scenes, start=1):
            scene.index = index

    scenes = _merge_similar_scenes(scenes)
    return scenes


def _merge_similar_scenes(scenes: list[Scene]) -> list[Scene]:
    if len(scenes) < 2:
        return scenes
    merged: list[Scene] = []
    for scene in scenes:
        if not merged:
            merged.append(scene)
            continue
        prev = merged[-1]
        prev_words = set(w.lower() for w in (prev.title + " " + prev.visual_direction).split() if len(w) > 3)
        curr_words = set(w.lower() for w in (scene.title + " " + scene.visual_direction).split() if len(w) > 3)
        if not prev_words or not curr_words:
            merged.append(scene)
            continue
        overlap = len(prev_words & curr_words)
        similarity = overlap / max(len(prev_words), len(curr_words))
        if similarity > 0.45:
            prev.end = scene.end
            prev.voice_text = (prev.voice_text + " " + scene.voice_text).strip()
            prev.retention_note = (prev.retention_note + "; " + scene.retention_note).strip()
        else:
            merged.append(scene)
    for i, scene in enumerate(merged, start=1):
        scene.index = i
    return merged


def call_lm_studio_for_shots(
    base_url: str,
    model: str,
    scenes: list[Scene],
    image_preference: str = "Auto",
    ttl_seconds: int | None = None,
    segments: list | None = None,
    on_scene_shots: Callable[[list[Shot]], None] | None = None,
    style_anchor: str = "",
    character_description: str = "",
    niche: str = "documentary",
    video_style: str = DEFAULT_VIDEO_STYLE,
    log_callback=None,
    hybrid: bool = False,
    route_priority: str = "balanced",
    ai_route_mix: bool = False,
    reference_density: str | None = None,
) -> list[Shot]:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    selected_style = get_video_style(video_style)
    style_brief = style_director_brief(video_style, style_anchor)

    # Reference-insert density: resolve from settings when the caller didn't pass
    # it, so every existing call site keeps working unchanged.
    if reference_density is None:
        try:
            from core.config import load_settings as _ls
            _s = _ls()
            reference_density = "off" if not getattr(_s, "reference_enable", True) \
                else str(getattr(_s, "reference_density", "normal"))
        except Exception:
            reference_density = "normal"
    reference_density = (reference_density or "normal").strip().lower()

    def _parse_shots(raw_shots: list[dict], offset: int = 0) -> list[Shot]:
        parsed: list[Shot] = []
        for raw in raw_shots:
            parsed.append(
                Shot(
                    index=len(parsed) + 1 + offset,
                    scene_index=int(raw.get("scene_index", 0)),
                    start=normalize_timestamp(raw.get("start", "00:00:00")),
                    end=normalize_timestamp(raw.get("end", raw.get("start", "00:00:00"))),
                    visual_type=str(raw.get("visual_type", "ai_image_then_i2v")).strip(),
                    description=str(raw.get("description", "")).strip(),
                    asset_query=str(raw.get("asset_query", "")).strip(),
                    image_model=str(raw.get("image_model", "")).strip(),
                    video_model=str(raw.get("video_model", "")).strip(),
                    image_prompt=str(raw.get("image_prompt", "")).strip(),
                    video_prompt=str(raw.get("video_prompt", "")).strip(),
                    screen_text=str(raw.get("screen_text", "")).strip(),
                    motion=str(raw.get("motion", "")).strip(),
                    priority=str(raw.get("priority", "medium")).strip(),
                    fallback=str(raw.get("fallback", "")).strip(),
                    sfx_query=str(raw.get("sfx_query", "")).strip(),
                    reference_query=str(raw.get("reference_query", "")).strip(),
                    reference_source=str(raw.get("reference_source", "wikipedia")).strip() or "wikipedia",
                )
            )
        return parsed

    def _lm_chat(prompt: str) -> list[dict]:
        payload = {
            "model": model.strip() or "local-model",
            "messages": [
                {"role": "system", "content": "Отвечай только валидным JSON. Не добавляй markdown. Описывай только буквальные физические сцены, без метафор и символов."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.25,
        }
        add_lm_ttl(payload, ttl_seconds)
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"].strip()
        return parse_lm_json_array(content, "LM Studio frame list")

    # Continuation variants for split parts: the same scene seen differently.
    # A split used to trigger a separate LLM sub-call with a WEAKER rule set —
    # those parts were systematically the worst shots of the video. Inheriting
    # part A's prompt with a deterministic re-framing is cheaper, editorially
    # correct (a continuation IS the same scene from another angle) and keeps
    # location/lighting/sfx continuity for free.
    _CONTINUATION_VARIANTS = [
        ("a closer medium view of the same subject",
         "The camera holds a closer framing of the same subject; small natural motion continues."),
        ("the same scene from a noticeably different side angle",
         "The camera observes from a new side angle; gentle drift, the action continues."),
        ("a detail close-up of the key object in the same scene",
         "A tight detail view; subtle ambient motion, slow push-in."),
        ("a slightly wider view of the same scene",
         "The camera pulls back slightly to a wider view; calm ambient motion."),
    ]

    def _continuation_shot(shot: Shot, start_s: float, end_s: float, depth: int) -> Shot:
        angle, motion = _CONTINUATION_VARIANTS[(shot.index + depth) % len(_CONTINUATION_VARIANTS)]
        data = dict(vars(shot))
        data.update(
            start=seconds_to_timecode(start_s),
            end=seconds_to_timecode(end_s),
            description=(shot.description + " (продолжение, другой ракурс)").strip(),
            image_prompt=(shot.image_prompt or shot.description).strip()
                         + f" Continuation shot of the same scene and location: {angle}.",
            video_prompt=motion,
            motion=motion,
            screen_text="",  # the overlay (if any) belongs to part A only
        )
        return Shot(**data)

    def _split_long_shot(shot: Shot, scene: Scene, scene_segs: list, depth: int = 0) -> list[Shot]:
        start_s = timecode_to_seconds(shot.start)
        end_s = timecode_to_seconds(shot.end)
        duration = end_s - start_s

        if duration <= SHOT_MAX_SECONDS:
            return [shot]

        shot_segs = [
            s for s in scene_segs
            if timecode_to_seconds(s.start) < end_s and timecode_to_seconds(s.end) > start_s
        ]

        if len(shot_segs) < 2:
            return [shot]

        max_end = start_s + SHOT_MAX_SECONDS + 2
        split_at = start_s + SHOT_MAX_SECONDS
        for s in shot_segs:
            seg_end = timecode_to_seconds(s.end)
            if seg_end <= max_end and seg_end > split_at:
                split_at = seg_end

        if split_at <= start_s or split_at >= end_s - 1:
            return [shot]

        part_a_data = dict(vars(shot))
        part_a_data.update(end=seconds_to_timecode(split_at))
        part_a = Shot(**part_a_data)

        # Part B: deterministic continuation of part A (no LLM sub-call).
        part_b = _continuation_shot(shot, split_at, end_s, depth)

        result = [part_a]
        result.extend(_split_long_shot(part_b, scene, scene_segs, depth + 1))
        return result

    route_priority = (route_priority or "balanced").strip().lower()
    if route_priority not in {"economy", "balanced", "rich"}:
        route_priority = "balanced"

    if route_priority == "economy":
        route_bias = (
            "ПРИОРИТЕТ МАРШРУТА: ЭКОНОМНО. По умолчанию предпочитай stock_video для любой реально снимаемой сцены. "
            "still_kenburns используй для явной статики, объектов, зданий, портретов, схем и исторических сцен без нужного движения. "
            "ai_image_then_i2v — крайний случай: только повторяющийся персонаж, выбранная стилистика, неснимаемое или кадр, где живое движение критично. "
            "ЖЁСТКИЙ БЮДЖЕТ: не больше 1 кадра из 5 и не больше 20% общей длительности — ai_image_then_i2v; "
            "считай длительность, а не количество: один длинный AI-кадр дороже трёх коротких стоковых. "
            "Самолёты, небо, грозы, города, люди, техника, природа — это ВСЕГДА stock_video, даже в драматичные моменты. "
            "Не выбирай AI-видео ради красоты, если сток или зум передадут смысл.\n"
        )
    elif route_priority == "rich":
        route_bias = (
            "ПРИОРИТЕТ МАРШРУТА: БОГАТО. Чаще выбирай ai_image_then_i2v для эмоционально важных, стилевых, персонажных и визуально выразительных кадров. "
            "stock_video используй для нейтрального b-roll и очевидно реальных сцен без требований к персонажу/стилю. "
            "still_kenburns используй только когда статичная картинка с медленным зумом действительно лучше движения. "
            "Сохраняй разнообразие, но не экономь AI-видео на ключевых моментах.\n"
        )
    else:
        route_bias = (
            "ПРИОРИТЕТ МАРШРУТА: СБАЛАНСИРОВАННО. Выбирай самый дешёвый маршрут, который хорошо передаёт смысл: "
            "снимаемое — stock_video, статичное — still_kenburns, стилевое/неснимаемое/важное движение — ai_image_then_i2v.\n"
        )

    if hybrid:
        route_block = (
            "РЕЖИМ РАСПРЕДЕЛЕНИЯ (гибрид). image_model=krea2_turbo. Для КАЖДОГО кадра САМ выбери visual_type:\n"
            f"{route_bias}"
            "- stock_video — реальная нейтральная СНИМАЕМАЯ сцена без конкретного персонажа "
            "(город, природа, руки за работой, техника, толпа, транспорт). Длина такого кадра ≤5 секунд.\n"
            "asset_query ОБЯЗАТЕЛЕН для КАЖДОГО кадра, включая ai_image_then_i2v и still_kenburns — "
            "короткая английская поисковая фраза (субъект + место): она нужна, чтобы кадр можно было "
            "перевести в сток при нехватке бюджета.\n"
            "- still_kenburns — статичный субъект, где движение не нужно (пейзаж, портрет, объект, здание, "
            "историческая сцена, число/схема): будет Krea-картинка с медленным зумом. video_model оставь пустым.\n"
            "- ai_image_then_i2v — стилистика, конкретный повторяющийся персонаж или НЕснимаемое, где реально "
            "нужно живое движение. Самый дорогой маршрут — только там, где он оправдан. video_model=ltx2.3_i2v.\n"
            "Чередуй маршруты для разнообразия; не ставь один и тот же подряд без нужды.\n\n"
        )
    elif ai_route_mix and route_priority != "rich":
        # AI mode with distribution: no stock (the style must stay fully
        # generated), but animation (LTX) is spent only where motion carries
        # meaning; static-by-nature shots become Krea stills with a slow zoom.
        if route_priority == "economy":
            ai_bias = (
                "ПРИОРИТЕТ: ЭКОНОМНО. Не больше 1 кадра из 4 и не больше ~25% общей длительности — "
                "ai_image_then_i2v. Считай длительность, а не количество: один длинный AI-кадр дороже "
                "трёх коротких. Всё статичное по природе — still_kenburns.\n"
            )
        else:
            ai_bias = (
                "ПРИОРИТЕТ: СБАЛАНСИРОВАННО. Примерно половина хронометража — still_kenburns "
                "(статика с зумом), половина — ai_image_then_i2v там, где движение реально работает.\n"
            )
        route_block = (
            "РЕЖИМ РАСПРЕДЕЛЕНИЯ (только AI, БЕЗ стока). image_model=krea2_turbo для каждого кадра. "
            "Для КАЖДОГО кадра САМ выбери visual_type:\n"
            f"{ai_bias}"
            "- still_kenburns — кадр статичен по природе: установочный план локации, объект, здание, "
            "портрет без действия, схема, «историческое фото». Krea-картинка с медленным зумом/паном. "
            "video_model оставь пустым.\n"
            "- ai_image_then_i2v — движение несёт СМЫСЛ или ЭМОЦИЮ: субъект совершает важное действие, "
            "среда в движении — суть кадра (огонь, дождь, вода, дым, толпа), хук, кульминация, "
            "кадры с priority=high. video_model=ltx2.3_i2v.\n"
            "Не ставь больше ТРЁХ still_kenburns подряд — между сериями статики вставляй живой кадр, "
            "иначе видео превращается в слайдшоу.\n\n"
        )
    else:
        route_block = (
            "Каждый shot — AI image-to-video: Krea2 генерирует изображение, LTX 2.3 анимирует его. "
            "Для КАЖДОГО кадра image_model=krea2_turbo, video_model=ltx2.3_i2v, visual_type=ai_image_then_i2v.\n\n"
        )

    # Reference-insert block: lets the director show a real Wikipedia/arXiv page
    # sliding in, for a concrete checkable entity. Density caps how often.
    if reference_density == "off":
        reference_block = ""
    else:
        cap_line = (
            "МАКСИМУМ 1 такой кадр на 2 сцены, только для САМОЙ важной названной сущности во всём видео-фрагменте."
            if reference_density == "sparse" else
            "МАКСИМУМ ~1 такой кадр на сцену, и только когда сущность действительно называется в озвучке."
        )
        reference_block = (
            "ДОПОЛНИТЕЛЬНЫЙ ТИП КАДРА — web_reference_card (реальный скрин источника, выезжает сбоку "
            "на размытом фоне). Ставь его ТОЛЬКО когда в озвучке названа ИЗВЕСТНАЯ проверяемая сущность, "
            "у которой ТОЧНО есть статья в энциклопедии: крупная компания, известный человек, "
            "устоявшаяся технология или стандарт, научная статья/paper, общепринятый термин или понятие. "
            "Это заодно даёт настоящий читаемый текст и ссылку-пруф.\n"
            "НЕ используй его для нишевых приложений, CLI-утилит и небольших опенсорс-инструментов "
            "(например LM Studio, Ollama, llama.cpp, GGUF, конкретные репозитории) — у них обычно нет "
            "статьи в Википедии, и выйдет случайный нерелевантный результат. Для таких — обычный визуал "
            "(скрин интерфейса как Krea-картинка).\n"
            f"{cap_line} Не используй его для абстракций, эмоций и общих сцен — там обычный визуал.\n"
            "Для такого кадра задай: visual_type=web_reference_card; reference_query = точное название "
            "сущности для поиска (для Википедии — на языке озвучки или по-английски; для статьи — её "
            "название); reference_source = \"wikipedia\" (по умолчанию) или \"arxiv\" для научной статьи. "
            "image_model и video_model оставь пустыми. ОБЯЗАТЕЛЬНО всё равно заполни asset_query и "
            "image_prompt — это резерв, если источник не найдётся.\n\n"
        )

    INSTRUCTIONS = (
        "Ты режиссёр: раскладываешь озвучку на кадры.\n\n"
        "Верни только валидный JSON-массив. Каждый объект должен иметь поля: "
        "scene_index, start, end, visual_type, description, asset_query, image_model, video_model, "
        "image_prompt, video_prompt, screen_text, motion, priority, fallback, sfx_query, "
        "reference_query, reference_source.\n\n"
        f"{route_block}"
        f"{reference_block}"
        f"Настройка пользователя: {image_preference}.\n\n"
        f"ВЫБРАННЫЙ СТИЛЬ: {selected_style.name}\n"
        f"{style_brief}\n\n"
        "ПРАВИЛА ДЛЯ image_prompt (Krea 2 — текстовое изображение):\n"
        "- Пиши на английском, длинные детальные промпты (3-6 предложений), естественным языком, а не тегами.\n"
        "- Krea 2 понимает описательные предложения лучше, чем список ключевых слов через запятую.\n"
        "- Описывай: shot type, subject, texture/material, lighting, color palette, composition, atmosphere.\n"
        "- Сначала опиши конкретный объект/сцену, затем детали выбранного стиля. Не используй LoRA/trigger слова.\n"
        "- Каждая абстрактная мысль из текста должна стать видимым предметом или конкретным действием внутри выбранного стиля — без символов и метафор.\n"
        "- Показывай УЗНАВАЕМОЕ реальное место/объект; не полагайся на читаемый текст или логотипы в кадре (модель их коверкает) — передавай узнаваемость формами, цветами и ориентирами.\n"
        "- Чередуй планы между соседними кадрами. Предпочитай ОБЩИЙ и СРЕДНИЙ план, на котором видно место; крупные планы и макро — изредка, не по умолчанию. Не ставь два одинаковых плана подряд.\n"
        "- Konкретные материалы и текстуры выбирай из выбранного стиля. Для вязаного стиля: yarn, wool fibers, visible stitches, crocheted props, handmade textile surfaces.\n"
        "- Konкретное освещение: 'golden hour backlight', 'cold fluorescent overhead', "
        "'warm tungsten desk lamp', 'harsh noon sun casting hard shadows'.\n"
        "- ЗАПРЕЩЕНО начинать промпт с абстрактного жанра: 'Documentary...', 'Cinematic...', "
        "'Photorealistic...', 'Editorial...', 'Observational...' — это абстракция, а не промпт.\n"
        "- Промт должен начинаться с КОНКРЕТНОГО ОБЪЕКТА ИЛИ СЦЕНЫ: "
        "'A weathered wooden door...', 'Rain streaming down a window pane...', "
        "'A middle-aged man in a stained apron...', 'Dust motes floating in...'.\n"
        "- Избегай клише: 'cinematic lighting' и 'photorealistic' — "
        "используй конкретные названия источников света и материалов.\n"
        "- Не описывай жанр видео. Опиши ЧТО мы видим, а не КАК это снято.\n"
        "- Используй visual_direction из сцены как подсказку для визуального стиля.\n"
        "- Не добавляй текст, логотипы или субтитры на изображение.\n"
        "- Не заставляй кадр быть реалистичной съёмкой реального мира, если выбранный стиль декоративный или handmade.\n\n"
        "ПРИМЕР ХОРОШЕГО image_prompt (начинается с объекта, а не жанра):\n"
        "\"A 40-year-old man in a wrinkled denim shirt sits at a cluttered wooden desk, "
        "morning light streams through a dusty window casting long diagonal shadows, "
        "a cold cup of coffee and scattered papers cover the desk surface, "
        "his tired eyes focused on a glowing laptop screen, "
        "warm tungsten light from the desk lamp mixes with cool daylight, "
        "rough concrete wall texture visible behind him, documentary realism.\"\n\n"
        "ПРИМЕР ПЛОХОГО image_prompt (абстрактный, начинается с жанра — НЕ ДЕЛАЙ ТАК):\n"
        "\"Cinematic documentary scene of a man working at his desk, moody lighting, photorealistic.\"\n\n"
        "ПРАВИЛА ДЛЯ video_prompt (LTX 2.3 image-to-video):\n"
        "- Опиши ДВИЖЕНИЕ, которое реально подходит ЭТОМУ кадру. Картинка уже задаёт вид — не описывай внешность.\n"
        "- ГЛАВНОЕ ПРАВИЛО: если в кадре есть субъект/объект, который логично что-то делает — дай ОДНО маленькое конкретное действие "
        "(вязаная кукла наклоняет голову; индикатор загрузки крутится; рука поправляет предмет; лампа мигает). "
        "Если кадр по смыслу статичный (пейзаж, общий план, объект без действия) — НЕ выдумывай действие: опиши движение среды "
        "(свет, ткань, вода, частицы, пар) и лёгкое движение камеры. И то и другое — правильный ответ.\n"
        "- Всегда добавляй ОДНО движение среды и ОДНО движение камеры (present tense): "
        "'the camera slowly pushes in', 'gentle pan left', 'subtle tracking shot', 'slow dolly in'.\n"
        "- Движение маленькое и естественное: избегай резких жестов, бега, прыжков, сильной деформации объекта и всего, что может вызвать морфинг или плавление деталей.\n"
        "- НЕ оставляй video_prompt пустым и не пиши общими словами вроде 'brings the shot to life' — привяжи движение к тому, что видно в кадре.\n"
        "- НЕ описывай звук в video_prompt — для звука есть отдельное поле sfx_query.\n"
        "- 2-4 предложения в одном абзаце, на английском.\n\n"
        "ПРАВИЛА МАРШРУТИЗАЦИИ:\n"
        "- screen_text — ПОЧТИ ВСЕГДА ПУСТАЯ СТРОКА (\"\"). Заполняй ТОЛЬКО когда кадру нужна надпись-акцент: "
        "ключевая цифра/статистика, дата, имя, название, термин — 2-5 слов в стиле заголовка, на том же языке, что и диктор. "
        "ЗАПРЕЩЕНО копировать или пересказывать фразы диктора — зритель их и так слышит; надпись-эхо выглядит как мусор. "
        "Не больше ОДНОЙ надписи на сцену.\n"
        "  ХОРОШО: '1 удар на 1000 часов', '8 декабря 1963', 'Клетка Фарадея', '40 миллионов рейсов в год'.\n"
        "  ПЛОХО (не делай): 'И вот что интересно', 'вы почти наверняка сидели', 'конец истории', 'Это правда'.\n"
        "- description — заметка монтажёру на том же языке, что и текст диктора.\n"
        "- asset_query — короткая ЛИТЕРАЛЬНАЯ английская поисковая фраза (2-5 слов) для поиска СТОКОВОГО ВИДЕО: "
        "конкретный субъект + место/действие. БЕЗ стиля, БЕЗ метафор, БЕЗ жанровых слов "
        "(documentary/cinematic/photorealistic). Только по-английски, реальные снимаемые объекты. "
        "Примеры: 'server room rows of racks', 'ocean waves crashing rocks', 'scientist microscope laboratory', "
        "'busy city street traffic', 'hands typing on laptop'.\n"
        "- image_prompt и video_prompt на английском.\n"
        "- sfx_query — короткая английская фраза (2-5 слов): ФОНОВЫЙ ЗВУК места в кадре для SFX-генератора. "
        "Только среда/фон, БЕЗ музыки и БЕЗ речи. Примеры: 'server room fan hum', 'city traffic ambience', "
        "'waves crashing on rocks', 'quiet forest birds', 'rain on window'. Соседние кадры одной локации "
        "должны иметь ОДИНАКОВЫЙ sfx_query, чтобы звук не дёргался.\n"
        "- Любые схемы, маршруты, карты и технические объяснения показывай как реалистичный AI-видеоряд.\n"
        "- Не выдумывай факты, работай только с текстом сегментов.\n"
        "- Таймкод кадра: start = первого сегмента в группе, end = последнего.\n"
    )

    if segments and hasattr(segments[0], "start") and hasattr(segments[0], "text"):
        all_shots: list[Shot] = []
        # Inter-scene memory: each scene call is otherwise independent, which
        # caused two systematic defects — matching framings back-to-back at
        # scene joins, and "location drift" (the same server room described
        # differently in scene 3 and scene 12 renders as two different rooms).
        recent_tail: list[str] = []      # last shots of the previous scene
        location_bank: list[str] = []    # first sentences of used image_prompts
        first_scene_index = scenes[0].index if scenes else None
        for scene in scenes:
            scene_start_s = timecode_to_seconds(scene.start)
            scene_end_s = timecode_to_seconds(scene.end)
            scene_segs = [
                s for s in segments
                if scene_start_s <= timecode_to_seconds(s.start) <= scene_end_s
            ]
            if not scene_segs:
                continue
            seg_lines = "\n".join(
                f"  [{i}] {s.start} - {s.end} | \"{s.text}\""
                for i, s in enumerate(scene_segs, start=1)
            )
            n_segs = len(scene_segs)
            style_line = (
                f"ОБЯЗАТЕЛЬНЫЙ ВИЗУАЛЬНЫЙ СТИЛЬ: {selected_style.name}. Каждый image_prompt и video_prompt должен подчиняться этому стилю. Никаких LoRA и trigger-фраз, только текстовый prompt.\n"
            )
            char_line = f"Персонаж: {character_description}\n" if character_description else ""
            # Memory block: joins + location reuse.
            memory_block = ""
            if recent_tail:
                memory_block += (
                    "ПОСЛЕДНИЕ КАДРЫ ПРЕДЫДУЩЕЙ СЦЕНЫ (для монтажного стыка):\n"
                    + "\n".join(f"  - {t}" for t in recent_tail)
                    + "\nПервый кадр ЭТОЙ сцены не должен повторять план и субъект последнего кадра выше — смени крупность или ракурс.\n"
                )
            if location_bank:
                memory_block += (
                    "УЖЕ ПОКАЗАННЫЕ ЛОКАЦИИ РОЛИКА (если действие происходит там же — описывай локацию ТЕМИ ЖЕ словами, не выдумывай её заново):\n"
                    + "\n".join(f"  - {loc}" for loc in location_bank[-8:])
                    + "\n"
                )
            if memory_block:
                memory_block += "\n"
            # The opening scene is the hook — the first shots decide retention.
            hook_block = ""
            if scene.index == first_scene_index:
                hook_block = (
                    "ЭТО ПЕРВАЯ СЦЕНА — ХУК РОЛИКА. Первые кадры решают, останется ли зритель:\n"
                    "- Первый кадр — самый сильный, интригующий визуал сцены, СРАЗУ по сути. НЕ нейтральная establishing-заставка.\n"
                    "- В первых кадрах обязательно движение или действие.\n"
                    "- Поставь priority=high первым 2-3 кадрам.\n\n"
                )
            scene_len = max(1, scene_end_s - scene_start_s)
            recommended = max(1, min(6, round(scene_len / 5)))
            consistency_line = (
                " - Сохраняй консистентность: персонаж/объект/стиль одинаковы во всех кадрах\n"
                if (style_anchor or character_description) else ""
            )
            prompt = (
                f"Ты visual director для YouTube-ролика.\n"
                f"Сцена {scene.index}: \"{scene.title}\"\n"
                f"Направление: {scene.visual_direction}\n"
                f"Надпись: {scene.screen_text}\n"
                f"{style_line}{char_line}\n"
                f"{memory_block}"
                f"{hook_block}"
                f"Сегменты этой сцены ({n_segs} шт.):\n{seg_lines}\n\n"
                "ШАГ 1 — РАЗВЕРНИ текст в полную визуальную сцену:\n"
                "  Из короткой фразы сделай конкретную сцену: "
                "какая локация, время суток, освещение, кто/что в кадре, "
                "какие материалы и текстуры, какая атмосфера.\n"
                "  Пример: 'он просыпается и берёт телефон' → "
                "спальня, утро, солнечный свет сквозь шторы, "
                "мужчина 30 лет в футболке тянется к телефону на тумбочке, "
                "мятое одеяло, деревянная мебель, тёплый свет.\n\n"
                "ШАГ 2 — создай кадры с РАЗНЫМИ ракурсами:\n"
                "- Каждый кадр = уникальная визуальная идея, пригодная для короткой image-to-video анимации\n"
                "- Чередуй shot types (общий → деталь → средний → неожиданный)\n"
                f"{consistency_line}"
                "- Не повторяй один концепт дважды в одной сцене\n"
                "- Не пытайся покрыть каждый сегмент — покрывай каждую СМЫСЛОВУЮ ЕДИНИЦУ\n"
                f"- Эта сцена длится {scene_len}с — сделай примерно {recommended} кадр(а/ов), ±1. "
                f"Не делай кадры длиннее {SHOT_MAX_SECONDS}с (LTX limit): лучше больше коротких, чем один длинный\n"
                "- Порядок кадров: от общего к частному или по логике повествования\n\n"
                f"{INSTRUCTIONS}"
            )
            # Resilient per-scene generation: retry once on bad/truncated JSON,
            # then fall back to heuristic shots for THIS scene and keep going —
            # one flaky scene must not abort the whole shot list.
            scene_shots: list[Shot] | None = None
            weak_motion = False
            for attempt in range(2):
                try:
                    extra = ""
                    if attempt == 1:
                        extra = (
                            "\n\nВЕРНИ СТРОГО валидный JSON-массив и НИЧЕГО больше: "
                            "без markdown, без текста до или после массива, все строки в кавычках."
                        )
                        if weak_motion:
                            extra += (
                                "\n\nВАЖНО про video_prompt: в КАЖДОМ кадре опиши конкретное движение, "
                                "подходящее именно этому кадру — одно маленькое действие субъекта, если в кадре есть "
                                "кто/что действует, иначе движение среды (свет, ткань, вода, частицы) плюс лёгкое "
                                "движение камеры. Не оставляй поле пустым и не пиши общими фразами."
                            )
                    raw = _lm_chat(prompt + extra)
                    parsed = _parse_shots(raw, offset=len(all_shots))
                    if parsed:
                        # Soft motion gate: if the model left motion empty/boilerplate on
                        # the first try, keep this result as a fallback but retry ONCE
                        # asking specifically for concrete per-shot motion.
                        if attempt == 0 and _motions_mostly_weak(parsed):
                            scene_shots = parsed
                            weak_motion = True
                            continue
                        scene_shots = parsed
                        break
                except Exception:
                    pass
            if not scene_shots:
                if log_callback:
                    log_callback(f"Сцена {scene.index} \"{scene.title}\": LLM вернул битый JSON — использую запасную раскадровку.")
                scene_shots = build_heuristic_shots([scene], image_preference=image_preference, style_anchor=style_anchor, video_style=video_style, character_description=character_description, compose=False)

            for s in scene_shots:
                s.scene_index = scene.index
            split_shots = []
            for s in scene_shots:
                split_shots.extend(_split_long_shot(s, scene, scene_segs))
            for i, s in enumerate(split_shots):
                s.index = len(all_shots) + i + 1
            all_shots.extend(split_shots)
            # Update the inter-scene memory for the NEXT scene's prompt.
            recent_tail = [
                (s.description or s.image_prompt)[:110].strip()
                for s in split_shots[-2:] if (s.description or s.image_prompt)
            ]
            for s in split_shots:
                first_sentence = (s.image_prompt or "").split(".")[0].strip()[:120]
                if first_sentence and "continuation shot" not in first_sentence.lower():
                    key = first_sentence.lower()
                    if all(key != loc.lower() for loc in location_bank):
                        location_bank.append(first_sentence)
            if on_scene_shots:
                on_scene_shots(split_shots)
        shots = all_shots
    else:
        compact_scenes = [
            {
                "scene_index": scene.index,
                "start": scene.start,
                "end": scene.end,
                "title": scene.title,
                "voice_text": scene.voice_text,
                "visual_direction": scene.visual_direction,
                "screen_text": scene.screen_text,
            }
            for scene in scenes
        ]
        prompt = (
            "Ты visual director для документального YouTube-ролика. На основе сцен создай shot list: "
            "конкретные кадры видеоряда длительностью 3-5 секунд.\n"
            "Главный принцип: один shot = один визуальный прием. "
            "Для документального стиля нужны частые смены кадров — минимум 2-3 кадра на каждые 10 секунд сцены.\n"
            f"{INSTRUCTIONS}"
            f"Сцены JSON:\n{json.dumps(compact_scenes, ensure_ascii=False)}"
        )
        raw_shots = _lm_chat(prompt)
        shots = _parse_shots(raw_shots)

    shots = clamp_shots_to_scenes(shots, scenes)
    # Re-tile timecodes so shots run back-to-back with no gaps/overlaps within the
    # timeline (root fix: the LLM assigns loose per-shot times; here we make them a
    # clean partition so the board, timeline preview and final render all agree).
    scenes_end = max((timecode_to_seconds(sc.end) for sc in scenes), default=None) if scenes else None
    shots = retile_shots_contiguous(shots, total_end=scenes_end)
    diversify_repeated_shots(shots, log_callback=log_callback)
    if hybrid:
        # The textual route bias alone is not enough: LLMs "alternate" routes by
        # COUNT while AI shots stay long and stock is later cut to <=5s pieces, so
        # AI quietly eats most of the RUNTIME. Enforce the budget deterministically.
        enforce_route_budget(shots, route_priority, log_callback=log_callback)
    elif ai_route_mix:
        enforce_ai_route_budget(shots, route_priority, log_callback=log_callback)
    normalize_shot_routes(shots, image_preference=image_preference)
    repair_shot_prompts(shots)
    # Enforce the selected style on every frame, regardless of how the LLM
    # phrased the prompt. Styles are pure text presets: no LoRA and no trigger.
    for s in shots:
        s.image_prompt = compose_image_prompt(
            video_style,
            s.image_prompt or s.description,
            character_description=character_description,
            custom_notes=style_anchor,
        )
        # If the model gave no usable motion, fall back to an honest form-based
        # default derived from the shot type (via make_ltx_prompt) rather than one
        # generic line for every clip.
        s.video_prompt = compose_video_prompt(
            video_style,
            s.video_prompt or s.motion or make_ltx_prompt(s.image_prompt),
            shot_description=s.description,
        )
    return shots



def retile_shots_contiguous(shots: list[Shot], total_end: int | None = None) -> list[Shot]:
    """Rewrite shot start/end so the shots tile the timeline with NO gaps and NO
    overlaps: each shot runs until the next shot's start, the first covers the
    lead-in from 0, and the last runs to total_end. Only the timecodes change —
    generated media stays linked by shot index/output path, so this is safe to run
    on an already-generated project without regenerating anything."""
    if not shots:
        return shots
    ordered = sorted(shots, key=lambda s: timecode_to_seconds(s.start))
    starts = [timecode_to_seconds(s.start) for s in ordered]
    if total_end is None:
        last_end = max((timecode_to_seconds(s.end) for s in ordered), default=0)
        total_end = max(last_end, starts[-1] + 1)
    intervals = tiled_intervals(starts, total_end)
    for index, (shot, (start_sec, end_sec)) in enumerate(zip(ordered, intervals), start=1):
        shot.start = seconds_to_timecode(start_sec)
        shot.end = seconds_to_timecode(end_sec)
        shot.index = index
    return ordered


def clamp_shots_to_scenes(shots: list[Shot], scenes: list[Scene]) -> list[Shot]:
    scene_by_index = {scene.index: scene for scene in scenes}
    clamped: list[Shot] = []

    for shot in shots:
        scene = scene_by_index.get(shot.scene_index)
        if not scene:
            shot.index = len(clamped) + 1
            clamped.append(shot)
            continue

        scene_start = timecode_to_seconds(scene.start)
        scene_end = max(scene_start + 1, timecode_to_seconds(scene.end))
        start_seconds = timecode_to_seconds(shot.start)
        end_seconds = timecode_to_seconds(shot.end)

        start_seconds = min(max(start_seconds, scene_start), scene_end - 1)
        end_seconds = min(max(end_seconds, start_seconds + 1), scene_end)
        if end_seconds <= start_seconds:
            end_seconds = min(scene_end, start_seconds + 1)

        shot.index = len(clamped) + 1
        shot.start = seconds_to_timecode(start_seconds)
        shot.end = seconds_to_timecode(end_seconds)
        clamped.append(shot)

    return clamped

def normalize_shot_routes(shots: list[Shot], image_preference: str = "Auto"):
    preferred_model = preferred_realistic_image_model(image_preference)
    preference = image_preference.strip().lower()
    for shot in shots:
        vt = (shot.visual_type or "").lower()
        # Stock route: leave the shot untouched (no AI image/video models).
        if vt == "stock_video":
            continue
        # Ken Burns route: needs a Krea still but NO LTX video (ffmpeg zoom builds the clip).
        if vt == "still_kenburns":
            if shot.image_model != "ideogram4" or preference in {"krea", "ideogram"}:
                shot.image_model = preferred_model
            shot.video_model = ""
            continue
        # Typography card renders its own image; leave the route as chosen.
        if vt == "ai_typography_card":
            continue
        # Reference-insert card: the backend screenshots a real page and builds
        # the clip itself — no Krea/LTX models, leave it alone.
        if vt == "web_reference_card":
            shot.image_model = ""
            shot.video_model = ""
            continue
        if shot.visual_type not in {"ai_image", "ai_image_then_i2v"}:
            shot.visual_type = "ai_image_then_i2v"
        if shot.image_model != "ideogram4" or preference in {"krea", "ideogram"}:
            shot.image_model = preferred_model
        shot.video_model = shot.video_model or "ltx2.3_i2v"
        if not shot.fallback or "Remotion" in shot.fallback or "SVG" in shot.fallback:
            shot.fallback = "AI image-to-video generation"
            if shot.visual_type == "ai_image":
                shot.visual_type = "ai_image_then_i2v"


# Compatibility seam for the desktop application and third-party extensions.
# Scene/shot analysis is canonical in headless core modules; assigning the
# established public names here keeps the desktop API stable while ensuring the
# web and desktop paths execute exactly the same implementation.
from core.scenes import (
    build_heuristic_scenes,
    build_scenes_by_embedding,
    build_scenes_from_segments,
    call_lm_studio,
    call_lm_studio_for_segments,
    enrich_scenes_with_lm,
    heuristic_music_note,
    heuristic_retention_note,
    heuristic_visual_direction,
    make_scene_from_segments,
    make_title,
)
from core.shot_planning import (
    build_heuristic_shots,
    call_lm_studio_for_shots,
    choose_visual_route,
    clamp_shots_to_scenes,
    clean_lm_json_content,
    clean_visual_prompt_source,
    make_asset_query,
    make_ltx_prompt,
    make_realistic_prompt,
    make_shot_description,
    motion_is_weak as _motion_is_weak,
    motions_mostly_weak as _motions_mostly_weak,
    normalize_shot_routes,
    parse_lm_json_array,
    preferred_realistic_image_model,
    prompt_has_too_much_transcript,
    rebuild_image_prompt_for_shot,
    repair_shot_prompts,
    retile_shots_contiguous,
    split_long_shot as _split_long_shot,
    suggest_motion as _suggest_motion,
    suggest_shot_type as _suggest_shot_type,
)

def explain_lm_studio_error(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace")
            message = f"{message}\n{body}"
        except Exception:
            pass
    lower = message.lower()
    if "exceeds the available context size" in lower or "context" in lower and "exceeds" in lower:
        return (
            "LM Studio отклонил запрос: вся транскрибация не помещается в текущий context length.\n"
            "Для полного анализа всего ролика одним запросом увеличь Context Length в LM Studio "
            "минимум до 32768, лучше 65536, и перезагрузи модель. "
            "Текущий режим нашей программы специально отправляет весь транскрипт целиком."
        )
    if "connection refused" in lower or "actively refused" in lower or "urlopen error" in lower:
        return "Не удалось подключиться к LM Studio. Проверь, что сервер запущен и URL указан правильно."
    return message


def lm_openai_to_rest_base(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if base.endswith("/api"):
        base = base[:-4]
    if base.endswith("/api/v1"):
        return base
    return base + "/api/v1"


def load_lm_studio_model(base_url: str, model: str, context_length: int) -> dict:
    endpoint = lm_openai_to_rest_base(base_url) + "/models/load"
    payload = {
        "model": model.strip(),
        "context_length": context_length,
        "flash_attention": True,
        "echo_load_config": True,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode("utf-8"))


def unload_lm_studio_model(base_url: str, instance_id: str) -> dict:
    endpoint = lm_openai_to_rest_base(base_url) + "/models/unload"
    payload = {"instance_id": instance_id.strip()}
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def write_srt(path: Path, scenes: list[Scene]):
    lines: list[str] = []
    for scene in scenes:
        start_seconds = timecode_to_seconds(scene.start)
        end_seconds = timecode_to_seconds(scene.end)
        text = re.sub(r"\s+", " ", scene.voice_text).strip()
        if len(text) > 180:
            text = text[:177].rsplit(" ", 1)[0] + "..."
        lines.extend(
            [
                str(scene.index),
                f"{seconds_to_srt_time(start_seconds)} --> {seconds_to_srt_time(end_seconds)}",
                text,
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def timecode_to_seconds(timecode: str) -> int:
    hours, minutes, seconds = [int(part) for part in timecode.split(":")]
    return hours * 3600 + minutes * 60 + seconds


def write_project_files(
    output_dir: Path,
    title: str,
    audio_path: Path | None,
    script_text: str,
    scenes: list[Scene],
    transcript_segments: list[TranscriptSegment] | None = None,
    whisper_json_path: Path | None = None,
    whisper_srt_path: Path | None = None,
):
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "assets").mkdir(exist_ok=True)
    (output_dir / "exports").mkdir(exist_ok=True)
    (output_dir / "notes").mkdir(exist_ok=True)

    if audio_path:
        audio_target = output_dir / "voice" / audio_path.name
        audio_target.parent.mkdir(exist_ok=True)
        shutil.copy2(audio_path, audio_target)

    (output_dir / "01_transcript.txt").write_text(clean_script_text(script_text), encoding="utf-8")
    if transcript_segments:
        (output_dir / "01_transcript_segments.json").write_text(
            json.dumps([segment.__dict__ for segment in transcript_segments], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if whisper_json_path and whisper_json_path.exists():
        shutil.copy2(whisper_json_path, output_dir / "01_whisper_raw.json")
    if whisper_srt_path and whisper_srt_path.exists():
        shutil.copy2(whisper_srt_path, output_dir / "04_subtitles_whisper.srt")
    (output_dir / "02_scenes.json").write_text(
        json.dumps([scene.__dict__ for scene in scenes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (output_dir / "03_edit_plan.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "scene",
                "start",
                "end",
                "title",
                "voice_text",
                "visual_direction",
                "screen_text",
                "music_note",
                "retention_note",
            ]
        )
        for scene in scenes:
            writer.writerow(
                [
                    scene.index,
                    scene.start,
                    scene.end,
                    scene.title,
                    scene.voice_text,
                    scene.visual_direction,
                    scene.screen_text,
                    scene.music_note,
                    scene.retention_note,
                ]
            )

    write_srt(output_dir / "04_subtitles_draft.srt", scenes)

    visual_lines = [f"# Visual Prompts: {title}", ""]
    music_lines = [f"# Music and SFX Notes: {title}", ""]
    for scene in scenes:
        visual_lines.extend(
            [
                f"## {scene.index:02d}. {scene.start}-{scene.end} - {scene.title}",
                scene.visual_direction,
                "",
                f"Screen text: {scene.screen_text}",
                "",
            ]
        )
        music_lines.extend(
            [
                f"## {scene.index:02d}. {scene.start}-{scene.end} - {scene.title}",
                scene.music_note,
                f"Retention: {scene.retention_note}",
                "",
            ]
        )

    (output_dir / "05_visual_prompts.md").write_text("\n".join(visual_lines), encoding="utf-8")
    (output_dir / "06_music_sfx_notes.md").write_text("\n".join(music_lines), encoding="utf-8")
    (output_dir / "07_youtube_package.md").write_text(
        "\n".join(
            [
                f"# {title}",
                "",
                "## Названия",
                f"- {title}",
                "- Как работает интернет на дне океана",
                "- Почему интернет держится на подводных кабелях",
                "",
                "## Описание",
                "Документальный ролик о том, как устроена физическая инфраструктура интернета: "
                "подводные кабели, маршруты, аварии, резервирование и роль технологических компаний.",
                "",
                "## Идеи Shorts",
                "- Почему интернет почти не работает через спутники",
                "- 5G не отменяет кабели",
                "- Главный враг интернета - якорь",
                "- Что будет, если порвется подводный кабель",
            ]
        ),
        encoding="utf-8",
    )


def write_transcription_project_files(
    project_dir: Path,
    audio_path: Path | None,
    transcript_text: str,
    transcript_segments: list[TranscriptSegment],
    whisper_json_path: Path | None,
    whisper_srt_path: Path | None,
):
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "voice").mkdir(exist_ok=True)
    (project_dir / "transcription").mkdir(exist_ok=True)
    (project_dir / "analysis").mkdir(exist_ok=True)
    (project_dir / "exports").mkdir(exist_ok=True)

    if audio_path and audio_path.exists():
        safe_copy(audio_path, project_dir / "voice" / audio_path.name)

    (project_dir / "01_transcript.txt").write_text(transcript_text.strip(), encoding="utf-8")
    (project_dir / "01_transcript_segments.json").write_text(
        json.dumps([segment.__dict__ for segment in transcript_segments], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if whisper_json_path and whisper_json_path.exists():
        safe_copy(whisper_json_path, project_dir / "01_whisper_raw.json")
        safe_copy(whisper_json_path, project_dir / "transcription" / whisper_json_path.name)
    if whisper_srt_path and whisper_srt_path.exists():
        safe_copy(whisper_srt_path, project_dir / "04_subtitles_whisper.srt")
        safe_copy(whisper_srt_path, project_dir / "transcription" / whisper_srt_path.name)


def safe_copy(source: Path, target: Path):
    source = source.resolve()
    target = target.resolve()
    if source == target:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def write_scene_analysis_files(project_dir: Path, scenes: list[Scene]):
    analysis_dir = project_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "02_scenes.json").write_text(
        json.dumps([scene.__dict__ for scene in scenes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (analysis_dir / "scene_analysis.json").write_text(
        json.dumps([scene.__dict__ for scene in scenes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (project_dir / "03_edit_plan.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "scene",
                "start",
                "end",
                "title",
                "voice_text",
                "visual_direction",
                "screen_text",
                "music_note",
                "retention_note",
            ]
        )
        for scene in scenes:
            writer.writerow(
                [
                    scene.index,
                    scene.start,
                    scene.end,
                    scene.title,
                    scene.voice_text,
                    scene.visual_direction,
                    scene.screen_text,
                    scene.music_note,
                    scene.retention_note,
                ]
            )


def write_shot_list_files(project_dir: Path, shots: list[Shot]):
    visual_dir = project_dir / "visual_plan"
    visual_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "04_shot_list.json").write_text(
        json.dumps([shot.__dict__ for shot in shots], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (visual_dir / "shot_list.json").write_text(
        json.dumps([shot.__dict__ for shot in shots], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (project_dir / "04_asset_requests.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "shot",
                "scene",
                "start",
                "end",
                "visual_type",
                "description",
                "asset_query",
                "image_model",
                "video_model",
                "image_prompt",
                "video_prompt",
                "screen_text",
                "motion",
                "priority",
                "fallback",
            ]
        )
        for shot in shots:
            writer.writerow(
                [
                    shot.index,
                    shot.scene_index,
                    shot.start,
                    shot.end,
                    shot.visual_type,
                    shot.description,
                    shot.asset_query,
                    shot.image_model,
                    shot.video_model,
                    shot.image_prompt,
                    shot.video_prompt,
                    shot.screen_text,
                    shot.motion,
                    shot.priority,
                    shot.fallback,
                ]
            )

    lines = ["# Visual Plan", ""]
    current_scene = None
    for shot in shots:
        if shot.scene_index != current_scene:
            current_scene = shot.scene_index
            lines.extend(["", f"## Scene {shot.scene_index}", ""])
        lines.extend(
            [
                f"### Кадр {shot.index:03d} | {shot.start}-{shot.end}",
                f"- Type: {shot.visual_type}",
                f"- Description: {shot.description}",
                f"- Asset query: {shot.asset_query}",
                f"- Image model: {shot.image_model or '-'}",
                f"- Video model: {shot.video_model or '-'}",
                f"- Screen text: {shot.screen_text or '-'}",
                f"- Motion: {shot.motion or '-'}",
                f"- Fallback: {shot.fallback or '-'}",
                "",
            ]
        )
        if shot.image_prompt:
            lines.extend(["Image prompt:", "", f"> {shot.image_prompt}", ""])
        if shot.video_prompt:
            lines.extend(["Video prompt:", "", f"> {shot.video_prompt}", ""])
    (project_dir / "05_visual_plan.md").write_text("\n".join(lines), encoding="utf-8")
    (visual_dir / "visual_plan.md").write_text("\n".join(lines), encoding="utf-8")


def write_remotion_jobs(project_dir: Path, shots: list[Shot]) -> list[Path]:
    jobs_dir = project_dir / "remotion_jobs"
    renders_dir = project_dir / "renders" / "remotion"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    renders_dir.mkdir(parents=True, exist_ok=True)
    for old_job in jobs_dir.glob("*.json"):
        old_job.unlink()
    created: list[Path] = []
    seen_templates: set[tuple[str, str]] = set()
    for shot in shots:
        job = remotion_job_for_shot(shot, renders_dir)
        if not job:
            continue
        dedupe_key = remotion_dedupe_key(job)
        if dedupe_key in seen_templates:
            continue
        seen_templates.add(dedupe_key)
        path = jobs_dir / f"shot_{shot.index:03d}_{job['props']['template']}.json"
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        created.append(path)
    return created


# Markers of vague/metaphorical SCENE DESCRIPTIONS (not prompt rules). Checked
# against the human-readable description, never against the composed image_prompt
# (which embeds style rules that legitimately mention these words).
_ABSTRACT_MARKERS = (
    "символизир", "олицетвор", "метафор", "как символ", "в виде символа",
    "представляет собой", "это образ", "abstract concept", "as a metaphor",
)


def is_concrete_prompt(text: str) -> bool:
    """Heuristic gate over a scene DESCRIPTION: concrete if it has enough words to
    describe a real scene and contains no metaphor markers. Conservative — only
    flags for review, never blocks. Empty text is treated as concrete (not flagged)."""
    t = (text or "").strip().lower()
    if not t:
        return True
    if len(t.split()) < 6:
        return False
    if any(marker in t for marker in _ABSTRACT_MARKERS):
        return False
    return True


# Words a narration fragment typically STARTS with. A real on-screen label
# (stat, date, name, term) never opens with a conjunction/pronoun/particle —
# but an echoed voice-over snippet almost always does.
_OVERLAY_STOPWORDS = {
    "и", "а", "но", "что", "чтобы", "это", "этот", "эта", "вот", "потому",
    "поэтому", "если", "когда", "как", "так", "то", "же", "ведь", "значит",
    "вы", "мы", "он", "она", "они", "оно", "я", "ты", "не", "ни", "или",
    "хотя", "просто", "теперь", "затем", "потом", "давайте", "начнём",
    "and", "but", "so", "because", "if", "when", "you", "we", "it", "this",
    "that", "the", "now", "then", "just",
}


def auto_overlay_for_screen_text(text: str) -> bool:
    """Decide whether a card's screen_text should be burned on screen as a
    drawtext overlay.

    Titles worth burning are stat/date/name/term callouts. Echoed narration
    fragments ("и вот что интересно", "конец истории") must stay off — the
    viewer already HEARS them, burning them reads as broken subtitles. Rules:
    a digit or mostly-uppercase phrase is a strong callout signal; otherwise
    only short (<=4 words) capitalized noun phrases that don't start like a
    sentence fragment qualify."""
    phrase = (text or "").strip()
    if not phrase:
        return False
    words = phrase.split()
    if not (1 <= len(words) <= 6) or len(phrase) > 48:
        return False
    # Strong callout signals: numbers (stats, dates) or an ALL-CAPS title.
    if any(ch.isdigit() for ch in phrase):
        return True
    letters = [c for c in phrase if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) >= 0.6:
        return True
    # Otherwise: short capitalized label that doesn't open like narration.
    if len(words) > 4:
        return False
    if "," in phrase:  # mid-sentence enumeration, not a label (digits passed above)
        return False
    if not phrase[0].isupper():
        return False
    if words[0].lower().strip(".,!?…") in _OVERLAY_STOPWORDS:
        return False
    # Trailing ellipsis/comma = cut-off sentence, not a label.
    if phrase.endswith(("...", "…", ",", "-", "—")):
        return False
    return True


def choose_text_overlay_layout(text: str, shot: Shot) -> tuple[str, str, float, float]:
    """Pick an automatic text overlay style and safe-ish position from text + shot
    metadata. This is intentionally deterministic (shot_index based), so rebuilding
    a board does not shuffle overlays around like confetti.
    """
    phrase = (text or "").strip()
    words = phrase.split()
    lower = f"{phrase} {shot.description} {shot.visual_type} {shot.asset_query}".lower()
    idx = int(getattr(shot, "index", 0) or 0)

    def side_pair(a: str, b: str) -> str:
        return a if idx % 2 else b

    is_number = any(ch.isdigit() for ch in phrase)
    is_upper = bool(phrase) and any(ch.isalpha() for ch in phrase) and (
        sum(1 for ch in phrase if ch.isalpha() and ch.isupper()) / max(1, sum(1 for ch in phrase if ch.isalpha()))
    ) >= 0.55
    is_error = any(w in lower for w in (
        "error", "warning", "failed", "failure", "limit", "throttle", "slow", "latency",
        "ошиб", "лимит", "предел", "сбой", "медлен", "задерж",
    ))
    is_terminal = any(w in lower for w in (
        "terminal", "console", "command", "cli", "code", "screen", "monitor", "benchmark",
        "тэрминал", "терминал", "команд", "код", "экран", "монитор", "бенчмарк",
    ))
    is_hands_or_desk = any(w in lower for w in (
        "hand", "hands", "keyboard", "desk", "laptop", "workstation", "рук", "клавиатур",
        "стол", "ноутбук", "рабоч",
    ))
    is_wide = any(w in lower for w in (
        "wide", "establishing", "landscape", "server rows", "data center", "datacenter",
        "общий", "панорам", "датацентр", "серверн",
    ))

    if is_error:
        return "warning", "top", 50.0, 12.0
    if is_number:
        return "stat", side_pair("top_left", "top_right"), 12.0 if idx % 2 else 88.0, 16.0
    if is_terminal and len(words) <= 4:
        return "terminal", side_pair("top_right", "top_left"), 88.0 if idx % 2 else 12.0, 14.0
    if is_upper or len(words) <= 3:
        return "badge", side_pair("top_left", "top_right"), 12.0 if idx % 2 else 88.0, 14.0
    if is_hands_or_desk:
        return "caption", side_pair("top_left", "top_right"), 12.0 if idx % 2 else 88.0, 18.0
    if is_wide:
        return "subtitle", side_pair("bottom_left", "bottom_right"), 12.0 if idx % 2 else 88.0, 86.0
    return "subtitle", ["bottom", "top_left", "top_right", "bottom_left", "bottom_right"][idx % 5], 50.0, 86.0


def build_asset_board(project_dir: Path, shots: list[Shot] | None = None) -> list[AssetCard]:
    scenes_path = project_dir / "02_scenes.json"
    if not scenes_path.exists():
        raise FileNotFoundError(f"Не найден файл сцен: {scenes_path}")
    if shots is None:
        shots_path = project_dir / "04_shot_list.json"
        if not shots_path.exists():
            raise FileNotFoundError(f"Не найден shot list: {shots_path}")
        shots = [Shot(**item) for item in json.loads(shots_path.read_text(encoding="utf-8"))]

    scenes = [Scene(**item) for item in json.loads(scenes_path.read_text(encoding="utf-8"))]
    normalize_shot_routes(shots)
    repair_shot_prompts(shots)
    scene_titles = {scene.index: scene.title for scene in scenes}
    existing = load_asset_board(project_dir)
    existing_by_shot = {card.shot_index: card for card in existing}
    cards: list[AssetCard] = []
    for shot in shots:
        old = existing_by_shot.get(shot.index)
        route = old.route if old and old.route else determine_asset_route(shot, "")
        image_model = old.image_model if old and old.image_model else shot.image_model
        video_model = old.video_model if old and old.video_model else shot.video_model
        image_output = old.image_output if old else find_existing_asset(project_dir, "images", shot.index)
        video_output = old.video_output if old else find_existing_asset(project_dir, "videos", shot.index)

        image_prompt = old.image_prompt if old and old.image_prompt else shot.image_prompt
        # Preserve real statuses (generated/approved/skipped/...); only the transient
        # pre-generation states are re-evaluated against the current validator, so
        # stale false-flags clear on rebuild.
        if old and (old.status or "").lower() not in ("planned", "needs_review", ""):
            status = old.status
            notes = old.notes
        elif not is_concrete_prompt(shot.description):
            status = "needs_review"
            notes = "Возможно слишком абстрактное/метафоричное описание сцены — проверьте."
        else:
            status = "planned"
            notes = ""

        screen_text = old.screen_text if old and old.screen_text else shot.screen_text

        # Text overlay: preserve any manual overlay settings the user made on an
        # existing card; for a fresh card, auto-enable a drawtext caption when
        # screen_text is a short key phrase. Export already renders this via
        # drawtext (see core/export.overlay_filter), so no extra generation cost.
        if old is not None:
            overlay_enabled = old.text_overlay_enabled
            # Re-evaluate AUTO-enabled overlays (no custom text = never touched
            # by the user) against the current heuristic, so a rebuild clears
            # stale narration-echo captions without losing manual choices.
            if overlay_enabled and not (old.text_overlay_text or "").strip():
                overlay_enabled = auto_overlay_for_screen_text(old.screen_text or shot.screen_text)
                auto_style, auto_position, auto_x, auto_y = choose_text_overlay_layout(old.screen_text or shot.screen_text, shot)
            else:
                auto_style, auto_position, auto_x, auto_y = (
                    old.text_overlay_style,
                    old.text_overlay_position,
                    old.text_overlay_x,
                    old.text_overlay_y,
                )
            overlay_text = old.text_overlay_text
            overlay_position = auto_position
            overlay_style = auto_style
            overlay_x = auto_x
            overlay_y = auto_y
            overlay_enter = old.text_overlay_enter
            overlay_exit = old.text_overlay_exit
        else:
            overlay_enabled = auto_overlay_for_screen_text(screen_text)
            overlay_style, overlay_position, overlay_x, overlay_y = choose_text_overlay_layout(screen_text, shot)
            overlay_text = ""  # empty -> export falls back to screen_text
            overlay_enter = "fade"
            overlay_exit = "fade"

        card = AssetCard(
                card_id=(old.card_id if old else str(uuid.uuid4())),
                shot_index=shot.index,
                scene_index=shot.scene_index,
                scene_title=scene_titles.get(shot.scene_index, f"Scene {shot.scene_index}"),
                start=shot.start,
                end=shot.end,
                visual_type=shot.visual_type,
                route=route,
                description=shot.description,
                asset_query=shot.asset_query,
                image_model=image_model,
                video_model=video_model,
                remotion_template="",
                image_prompt=image_prompt,
                video_prompt=old.video_prompt if old and old.video_prompt else shot.video_prompt,
                screen_text=screen_text,
                motion=shot.motion,
                fallback=shot.fallback,
                image_output=image_output,
                video_output=video_output,
                remotion_output="",
                status=status,
                notes=notes,
                approved=old.approved if old else False,
                text_overlay_enabled=overlay_enabled,
                text_overlay_text=overlay_text,
                text_overlay_position=overlay_position,
                text_overlay_style=overlay_style,
                text_overlay_x=overlay_x,
                text_overlay_y=overlay_y,
                text_overlay_enter=overlay_enter,
                text_overlay_exit=overlay_exit,
                sfx_query=(old.sfx_query if old and old.sfx_query else shot.sfx_query),
                sfx_output=(old.sfx_output if old else ""),
                priority=(shot.priority or "medium"),
                reference_query=getattr(shot, "reference_query", "") or "",
                reference_source=(getattr(shot, "reference_source", "") or "wikipedia"),
                reference_url=(old.reference_url if old else ""),
                reference_credit=(old.reference_credit if old else ""),
            )
        sync_asset_card_route(card)
        cards.append(card)
    # De-duplicate consecutive overlays: after stock splitting, one shot's
    # screen_text lands on every 5s piece — the same label flashing on three
    # cuts in a row reads as a glitch. Keep it on the first card only.
    prev_text = ""
    for card in cards:
        if not card.text_overlay_enabled:
            prev_text = ""
            continue
        text = (card.text_overlay_text or card.screen_text or "").strip().lower()
        if text and text == prev_text:
            card.text_overlay_enabled = False
        else:
            prev_text = text
    return cards


def determine_asset_route(shot: Shot, remotion_template: str = "") -> str:
    vt = (shot.visual_type or "").lower()
    if vt == "stock_video":
        return "stock"
    if vt == "still_kenburns":
        return "kenburns"
    if vt == "web_reference_card":
        return "reference"
    return "image+video" if shot.video_model else "image"


def sync_asset_card_route(card: AssetCard, image_preference: str = "Auto") -> AssetCard:
    """Keep route, visual_type and model fields consistent.

    Asset Board is edited from several places (shot rebuild, web UI, stock picker,
    timeline). Route is what the user sees; visual_type is what generation uses.
    This helper makes that contract explicit without deleting existing media.
    """
    route = (card.route or "").strip().lower()
    vt = (card.visual_type or "").strip().lower()
    if route not in {"stock", "kenburns", "reference", "image", "image+video"}:
        if vt == "stock_video":
            route = "stock"
        elif vt == "still_kenburns":
            route = "kenburns"
        elif vt == "web_reference_card":
            route = "reference"
        elif vt == "ai_image" or not card.video_model:
            route = "image"
        else:
            route = "image+video"

    preferred_model = preferred_realistic_image_model(image_preference)
    if route == "reference":
        card.route = "reference"
        card.visual_type = "web_reference_card"
        card.image_model = ""
        card.video_model = ""
    elif route == "stock":
        card.route = "stock"
        card.visual_type = "stock_video"
        card.image_model = ""
        card.video_model = ""
    elif route == "kenburns":
        card.route = "kenburns"
        card.visual_type = "still_kenburns"
        if not card.image_model:
            card.image_model = preferred_model
        card.video_model = ""
    elif route == "image":
        card.route = "image"
        card.visual_type = "ai_image"
        if not card.image_model:
            card.image_model = preferred_model
        card.video_model = ""
    else:
        card.route = "image+video"
        card.visual_type = "ai_image_then_i2v"
        if not card.image_model:
            card.image_model = preferred_model
        card.video_model = card.video_model or "ltx2.3_i2v"
    return card


def _enrich_template_props(template: str, title: str, subtitle: str, card: AssetCard) -> dict:
    """Return richer templateProps based on the template name and card data."""
    base = {
        "title": title,
        "subtitle": subtitle,
        "fps": 25,
        "backgroundColor": "#0F172A",
    }
    if template in ("HeroTitle", "SectionTitle"):
        return {**base, "title": title, "subtitle": subtitle, "accentColor": "#7C3AED"}
    if template == "StatCardV2":
        return {**base, "stat": title, "subtitle": subtitle, "accentColor": "#F59E0B"}
    if template in ("BarChart", "LineChart"):
        return {**base, "title": title, "chartData": [], "chartSeries": []}
    if template == "PieChart":
        return {**base, "title": title, "data": [], "donut": False, "showLegend": True}
    if template == "KPIGrid":
        return {**base, "title": title, "metrics": [], "columns": 3}
    if template == "StatReveal":
        return {**base, "data": [], "columns": 3, "accentColor": "#7C3AED"}
    if template == "CalloutBox":
        return {**base, "text": subtitle or title, "type": "info"}
    if template == "ComparisonCard":
        return {**base, "title": title, "leftLabel": subtitle, "rightLabel": card.description}
    if template == "ProgressBar":
        return {**base, "progress": 0.5, "label": title}
    if template == "TextCard":
        return {**base, "text": subtitle or title, "fontSize": 64, "color": "#F8FAFC"}
    if template == "TerminalScene":
        return {**base, "lines": [], "title": title}
    if template == "ScreenshotScene":
        return {**base, "title": title, "image": card.image_output or ""}
    if template == "TitledVideo":
        return {**base, "title": title, "src": card.video_output or ""}
    if template == "ProductReveal":
        return {**base, "title": title, "subtitle": subtitle}
    if template == "CollageBurst":
        return {**base, "title": title, "images": []}
    if template == "LyricOverlay":
        return {**base, "lyrics": []}
    if template == "CaptionOverlay":
        return {**base, "words": [], "highlightColor": "#7C3AED"}
    return {
        "template": template,
        "durationSeconds": min(
            max(2, timecode_to_seconds(card.end) - timecode_to_seconds(card.start)), 10
        ),
        "title": title,
        "subtitle": subtitle,
        "fps": 25,
    }


def build_cuts_array(project_dir: Path, cards: list[AssetCard] | None = None) -> dict:
    """Build a cuts array for Remotion VideoComposer from the asset board."""
    if cards is None:
        cards = load_asset_board(project_dir)
    if not cards:
        return {"cuts": [], "fps": 25}

    cuts = []
    has_audio = False

    for card in cards:
        # Respect the same exclusion as the final ffmpeg export so the rough
        # Remotion preview shows the same set of shots the user will ship.
        if (getattr(card, "status", "") or "").lower() in {"skipped", "rejected"}:
            continue
        in_sec = timecode_to_seconds(card.start)
        out_sec = timecode_to_seconds(card.end)
        if out_sec <= in_sec:
            out_sec = in_sec + 3
        cut_id = f"shot_{card.shot_index:03d}"

        if card.video_output and Path(card.video_output).exists():
            cuts.append({
                "id": cut_id,
                "type": "video",
                "source": str(Path(card.video_output).resolve()),
                "in_seconds": in_sec,
                "out_seconds": out_sec,
            })
        elif card.remotion_output and Path(card.remotion_output).exists():
            cuts.append({
                "id": cut_id,
                "type": "video",
                "source": str(Path(card.remotion_output).resolve()),
                "in_seconds": in_sec,
                "out_seconds": out_sec,
            })
        elif card.image_output and Path(card.image_output).exists():
            cuts.append({
                "id": cut_id,
                "type": "image",
                "source": str(Path(card.image_output).resolve()),
                "in_seconds": in_sec,
                "out_seconds": out_sec,
            })
        elif card.remotion_template:
            duration = max(2, out_sec - in_sec)
            title = card.screen_text.strip() or card.scene_title.strip() or card.description
            subtitle = card.description if card.screen_text else ""
            cuts.append({
                "id": cut_id,
                "type": "template",
                "template": card.remotion_template,
                "templateProps": _enrich_template_props(card.remotion_template, title, subtitle, card),
                "in_seconds": in_sec,
                "out_seconds": out_sec,
            })
        else:
            text = card.screen_text.strip() or card.scene_title.strip() or card.description.strip() or f"Shot {card.shot_index}"
            cuts.append({
                "id": cut_id,
                "type": "text",
                "text": text,
                "subtitle": card.description if card.screen_text else "",
                "in_seconds": in_sec,
                "out_seconds": out_sec,
            })

    return {
        "cuts": cuts,
        "fps": 25,
        "audio": [],
    }

def load_asset_board(project_dir: Path) -> list[AssetCard]:
    path = project_dir / "06_asset_board.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    cards: list[AssetCard] = []
    field_defaults = {
        "image_output": "",
        "video_output": "",
        "remotion_template": "",
        "remotion_output": "",
        "status": "planned",
        "notes": "",
        "approved": False,
        "timeline_start": "",
        "timeline_end": "",
        "assigned_media": "",
        "track": "video_1",
        "text_overlay_enabled": False,
        "text_overlay_text": "",
        "text_overlay_position": "bottom",
        "text_overlay_style": "subtitle",
        "text_overlay_x": 50.0,
        "text_overlay_y": 86.0,
        "text_overlay_enter": "fade",
        "text_overlay_exit": "fade",
    }
    for index, item in enumerate(raw):
        # Legacy boards did not persist an identity.  A deterministic UUID keeps
        # repeated read-only API calls stable until the next ordinary board save
        # writes it permanently.
        card_id = str(item.get("card_id", "") or "").strip()
        if not card_id:
            card_id = legacy_asset_card_id(
                project_dir, index, item.get("shot_index", "")
            )
        data = {**field_defaults, **item}
        data["card_id"] = card_id
        cards.append(sync_asset_card_route(AssetCard(**data)))
    return cards


def write_asset_board(project_dir: Path, cards: list[AssetCard]):
    # Atomic write: the board is written by generation, SFX, overlays and the
    # exporter — a torn write here corrupts the whole project's source of truth.
    from core import asset_variants
    from core.project import atomic_write_text, open_project

    # Bind schema-v1 provenance to the board's pre-write positions before a
    # reorder can make those legacy indices ambiguous.
    existing = load_asset_board(project_dir)
    asset_variants.bind_card_identities(
        project_dir,
        {index: card.card_id for index, card in enumerate(existing)},
    )
    seen_ids: set[str] = set()
    for card in cards:
        card_id = str(getattr(card, "card_id", "") or "").strip()
        if not card_id or card_id in seen_ids:
            card_id = str(uuid.uuid4())
            card.card_id = card_id
        seen_ids.add(card_id)
    board_data = [card.__dict__ for card in cards]
    atomic_write_text(
        project_dir / "06_asset_board.json",
        json.dumps(board_data, ensure_ascii=False, indent=2),
    )
    project = open_project(Path(project_dir))
    if project is not None:
        project.mark_stage_completed(
            "assets",
            board_data,
            input_hash=project.content_hash(project.read_json("shot_list") or []),
        )


def find_existing_asset(project_dir: Path, folder: str, shot_index: int) -> str:
    base = project_dir / "assets" / folder
    if not base.exists():
        return ""
    matches = sorted(base.glob(f"shot_{shot_index:03d}*"))
    return str(matches[0]) if matches else ""


# Compatibility exports for desktop call sites.  Asset Board persistence and
# reconciliation are canonical in core.board; backend modules import that
# module directly and no longer need Tk just to access board data.
from core.board import (
    build_asset_board,
    determine_asset_route,
    find_existing_asset,
    load_asset_board,
    sync_asset_card_route,
    write_asset_board,
)


def screen_text_has_number(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


def _ffmpeg_filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if re.match(r"^[A-Za-z]:/", value):
        value = value[0] + r"\:" + value[2:]
    return value.replace("'", r"\'")


def find_ffmpeg_path() -> str:
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            configured = Path(str(settings.get("ffmpeg", "")).strip())
            if configured.exists():
                return str(configured)
        except (json.JSONDecodeError, OSError):
            pass
    return shutil.which("ffmpeg.exe") or shutil.which("ffmpeg") or find_default_tool("ffmpeg.exe") or "ffmpeg"


def find_overlay_font() -> Path | None:
    for font_path in (
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\tahoma.ttf"),
    ):
        if font_path.exists():
            return font_path
    return None


def apply_number_overlay_to_video(video_path: Path, project_dir: Path, card: AssetCard, log_callback=None) -> Path:
    text = (card.screen_text or "").strip()
    if not screen_text_has_number(text):
        return video_path

    video_path = Path(video_path)
    overlay_dir = project_dir / "renders" / "overlays"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    text_path = overlay_dir / f"shot_{card.shot_index:03d}_overlay.txt"
    output_path = overlay_dir / f"shot_{card.shot_index:03d}_overlay.mp4"
    text_path.write_text(text, encoding="utf-8")

    duration = max(2.0, timecode_to_seconds(card.end) - timecode_to_seconds(card.start))
    fade_in = min(0.4, duration / 4)
    fade_out = min(0.5, duration / 4)
    fade_out_start = max(fade_in, duration - fade_out)
    alpha = (
        f"if(lt(t\\,{fade_in:.2f})\\,t/{fade_in:.2f}\\,"
        f"if(gt(t\\,{fade_out_start:.2f})\\,max(0\\,({duration:.2f}-t)/{fade_out:.2f})\\,1))"
    )
    text_file = _ffmpeg_filter_path(text_path)
    font = find_overlay_font()
    font_part = f"fontfile='{_ffmpeg_filter_path(font)}':" if font else ""
    filter_graph = (
        "drawbox=x=0:y=ih-190:w=iw:h=150:color=black@0.45:t=fill,"
        f"drawtext={font_part}textfile='{text_file}':fontcolor=white:fontsize=64:"
        f"borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-150:alpha='{alpha}'"
    )
    ffmpeg = find_ffmpeg_path()
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-vf",
        filter_graph,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        str(output_path),
    ]
    if log_callback:
        log_callback(f"Накладываю экранный текст с анимацией: {text}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "FFmpeg failed").strip()[-1200:]
        raise RuntimeError(f"Не удалось наложить screen_text через FFmpeg: {details}")
    return output_path

def _load_preset_remotion_config(renders_dir: Path) -> dict:
    """Load the active project's preset `remotion_templates` map.

    `renders_dir` is <project>/renders/remotion, so the project dir (and its
    `.project.json` with the chosen preset) is two levels up. This lets template
    selection be niche-configurable without changing any call sites.
    """
    try:
        project_dir = Path(renders_dir).resolve().parent.parent
        preset_name = "documentary"
        meta = project_dir / ".project.json"
        if meta.exists():
            preset_name = json.loads(meta.read_text("utf-8")).get("preset", preset_name) or preset_name
        pipeline_file = Path(__file__).resolve().parent / "presets" / preset_name / "pipeline.json"
        if pipeline_file.exists():
            raw = json.loads(pipeline_file.read_text("utf-8"))
            cfg = raw.get("remotion_templates")
            if isinstance(cfg, dict):
                return cfg
    except Exception:
        pass
    return {}


def _select_preset_template(text: str, templates_cfg: dict) -> str | None:
    """Pick a template by preset keywords, preferring higher `priority`, then
    the longest matching keyword."""
    best_name = None
    best_key = (-1, 0)
    for name, entry in templates_cfg.items():
        if not isinstance(entry, dict):
            continue
        priority = int(entry.get("priority", 0) or 0)
        for kw in entry.get("match", []):
            k = str(kw).lower().strip()
            if k and k in text:
                key = (priority, len(k))
                if key > best_key:
                    best_key = key
                    best_name = name
    return best_name


def _derive_flow_nodes(shot: Shot) -> list[str]:
    source = shot.screen_text or shot.description or ""
    parts = [p.strip() for p in re.split(r"\s*(?:→|->|,|;|·|→)\s*", source) if p.strip()]
    if len(parts) >= 2:
        return parts[:6]
    return ["Шаг 1", "Шаг 2", "Шаг 3"]


def _build_preset_template_props(name: str, entry: dict, shot: Shot, duration: int) -> dict | None:
    """Build Remotion props for a preset-selected template, merging preset
    defaults with the shot's own text. Returns None when the template needs
    niche-specific geometry the preset doesn't provide (caller then falls back
    to the built-in heuristics)."""
    dur = min(duration, int(entry.get("duration_max", 8) or 8))
    accent = entry.get("accent") or entry.get("default_accent")
    title = shot.screen_text.strip() or entry.get("title") or shot.description.strip()
    subtitle = entry.get("subtitle") or (shot.description.strip() if shot.screen_text else "")
    props: dict = {"template": name, "durationSeconds": dur}
    if accent:
        props["accent"] = accent

    if name == "StatCard":
        props.update({
            "title": entry.get("title", "Ключевой факт"),
            "value": extract_stat_value(shot.screen_text or shot.description),
            "label": shot.screen_text or shot.description,
        })
    elif name in {"MapRoute", "RerouteMap"}:
        route = entry.get("route")
        if not route:
            return None
        props.update({"title": title, "subtitle": subtitle, "route": route})
        if name == "RerouteMap":
            if not entry.get("alternateRoute"):
                return None
            props["alternateRoute"] = entry["alternateRoute"]
    elif name == "MapSplit":
        props.update({
            "title": title, "subtitle": subtitle,
            "leftLabel": entry.get("leftLabel", "A"),
            "rightLabel": entry.get("rightLabel", "B"),
        })
    elif name == "FlowDiagram":
        props.update({
            "title": title, "subtitle": subtitle,
            "nodes": entry.get("nodes") or _derive_flow_nodes(shot),
        })
    elif name == "ChapterTitle":
        props.update({
            "chapter": entry.get("chapter", f"Кадр {shot.index:03d}"),
            "title": title,
            "subtitle": subtitle,
        })
    else:  # TextOverlay and any custom/niche template
        props.update({"title": title, "subtitle": subtitle})
    return props


def _build_card_template_props(name: str, entry: dict, card: AssetCard, duration: int) -> dict | None:
    """Build Remotion props for a template from the CARD's own content, so each
    shot gets its own title/subtitle/data instead of fixed preset constants.
    Style (accent/duration) comes from the preset; geometry that can't be derived
    from text (map routes) still comes from the preset entry."""
    dur = min(duration, int(entry.get("duration_max", 8) or 8))
    accent = entry.get("accent") or entry.get("default_accent")
    title = (card.screen_text or "").strip() or entry.get("title") or (card.description or "").strip()
    subtitle = (card.description or "").strip() or entry.get("subtitle", "")
    props: dict = {"template": name, "durationSeconds": dur}
    if accent:
        props["accent"] = accent

    if name == "StatCard":
        props.update({
            "title": title or entry.get("title", "Ключевой факт"),
            "value": extract_stat_value(card.screen_text or card.description),
            "label": card.screen_text or card.description,
        })
    elif name in {"MapRoute", "RerouteMap"}:
        route = entry.get("route")
        if not route:
            return None
        props.update({"title": title, "subtitle": subtitle, "route": route})
        if name == "RerouteMap":
            if not entry.get("alternateRoute"):
                return None
            props["alternateRoute"] = entry["alternateRoute"]
    elif name == "MapSplit":
        props.update({
            "title": title, "subtitle": subtitle,
            "leftLabel": entry.get("leftLabel", "A"),
            "rightLabel": entry.get("rightLabel", "B"),
        })
    elif name == "FlowDiagram":
        props.update({"title": title, "subtitle": subtitle, "nodes": _derive_flow_nodes(card)})
    elif name == "ChapterTitle":
        props.update({"chapter": f"Кадр {card.shot_index:03d}", "title": title, "subtitle": subtitle})
    else:
        props.update({"title": title, "subtitle": subtitle})
    return props


def remotion_job_from_card(card: AssetCard, renders_dir: Path) -> dict | None:
    """Build a Remotion job straight from the card: honours the card's chosen
    `remotion_template` (or auto-selects by text) and fills props from the card's
    own text. This is what makes per-shot graphics differ instead of repeating
    the same fixed template instance."""
    duration = max(2, timecode_to_seconds(card.end) - timecode_to_seconds(card.start))
    templates_cfg = _load_preset_remotion_config(renders_dir)
    text = f"{card.description} {card.screen_text} {getattr(card, 'asset_query', '')}".lower()
    name = (getattr(card, "remotion_template", "") or "").strip() or _select_preset_template(text, templates_cfg)
    if not name:
        return None
    entry = templates_cfg.get(name, {})
    props = _build_card_template_props(name, entry, card, duration)
    if not props:
        return None
    return {
        "shot_index": card.shot_index,
        "scene_index": card.scene_index,
        "start": card.start,
        "end": card.end,
        "output": str((renders_dir / f"shot_{card.shot_index:03d}_{name}.mp4").resolve()),
        "props": props,
    }


def remotion_job_for_shot(shot: Shot, renders_dir: Path) -> dict | None:
    duration = max(2, timecode_to_seconds(shot.end) - timecode_to_seconds(shot.start))
    visual_type = shot.visual_type.lower()
    text = f"{shot.description} {shot.screen_text} {shot.asset_query}".lower()
    if visual_type in {"programmatic_schema", "remotion_graphic", "motion_text", "map_graphic"}:
        if any(word in text for word in ("hair", "human hair", "anatomy", "exploded", "cutaway", "cross-section", "owners", "logos")):
            return None

        # Preset-driven template selection (niche-configurable via
        # presets/<niche>/pipeline.json -> remotion_templates). Falls back to the
        # built-in documentary heuristics below when the preset has no match.
        _templates_cfg = _load_preset_remotion_config(renders_dir)
        _selected = _select_preset_template(text, _templates_cfg)
        _preset_props = (
            _build_preset_template_props(_selected, _templates_cfg[_selected], shot, duration)
            if _selected else None
        )
        if _preset_props:
            template = _preset_props["template"]
            props = _preset_props
        elif "bgp" in text or "обход" in text or "reroute" in text or "routing" in text or "маршрут" in text and "повреж" in text:
            template = "RerouteMap"
            props = {
                "template": template,
                "durationSeconds": min(duration, 8),
                "title": shot.screen_text or "Интернет ищет обходной путь",
                "subtitle": "Когда одна линия повреждена, трафик может уйти другим маршрутом",
                "route": [
                    {"x": 420, "y": 560, "label": "Вы"},
                    {"x": 720, "y": 468},
                    {"x": 1040, "y": 488},
                    {"x": 1400, "y": 560, "label": "Сайт"},
                ],
                "alternateRoute": [
                    {"x": 420, "y": 560, "label": "Вы"},
                    {"x": 660, "y": 660},
                    {"x": 1000, "y": 720},
                    {"x": 1290, "y": 660},
                    {"x": 1400, "y": 560, "label": "Сайт"},
                ],
            }
        elif "спут" in text and ("кабель" in text or "океан" in text):
            template = "MapSplit"
            props = {
                "template": template,
                "durationSeconds": min(duration, 7),
                "title": shot.screen_text or "Не космос, а океан",
                "subtitle": "Интернет кажется беспроводным, но держится на физической инфраструктуре",
                "leftLabel": "Реальность: кабели",
                "rightLabel": "Миф: спутники",
            }
        elif has_stat_value(shot.screen_text or shot.description):
            template = "StatCard"
            props = {
                "template": template,
                "durationSeconds": min(duration, 8),
                "title": "Ключевой факт",
                "value": extract_stat_value(shot.screen_text or shot.description),
                "label": shot.screen_text or shot.description,
            }
        elif any(word in text for word in ("атлант", "карта", "map", "route", "borders", "classified", "redacted", "chokepoint")):
            template = "MapRoute"
            props = {
                "template": template,
                "durationSeconds": min(duration, 8),
                "title": shot.screen_text or "Данные пересекают океан",
                "subtitle": "Маршрут выглядит цифровым, но проходит через физический кабель",
                "route": [
                    {"x": 1130, "y": 470, "label": "Европа"},
                    {"x": 960, "y": 425},
                    {"x": 760, "y": 455},
                    {"x": 600, "y": 500, "label": "США"},
                ],
            }
        elif "5g" in text or "вышк" in text:
            template = "FlowDiagram"
            props = {
                "template": template,
                "durationSeconds": min(duration, 10),
                "title": shot.screen_text or "5G не отменяет кабели",
                "subtitle": "Беспроводным остается только последний участок до вышки",
                "nodes": ["Телефон", "5G-вышка", "Оптоволокно", "Дата-центр", "Подводный кабель"],
            }
        else:
            return None
    elif visual_type == "ai_typography_card":
        template = "ChapterTitle"
        props = {
            "template": template,
            "durationSeconds": min(duration, 7),
            "chapter": f"Кадр {shot.index:03d}",
            "title": shot.screen_text or shot.description,
            "subtitle": shot.description if shot.screen_text else "",
        }
    else:
        return None

    return {
        "shot_index": shot.index,
        "scene_index": shot.scene_index,
        "start": shot.start,
        "end": shot.end,
        "output": str((renders_dir / f"shot_{shot.index:03d}_{template}.mp4").resolve()),
        "props": props,
    }


def remotion_motion_text_job_for_card(card: AssetCard, renders_dir: Path) -> dict:
    duration = max(2, timecode_to_seconds(card.end) - timecode_to_seconds(card.start))
    title = card.screen_text.strip() or card.scene_title.strip() or short_title_from_description(card.description)
    subtitle = card.description.strip()
    if subtitle.lower().startswith(("supporting b-roll", "closing visual")):
        subtitle = ""
    template = "ChapterTitle"
    return {
        "shot_index": card.shot_index,
        "scene_index": card.scene_index,
        "start": card.start,
        "end": card.end,
        "output": str((renders_dir / f"shot_{card.shot_index:03d}_{template}.mp4").resolve()),
        "props": {
            "template": template,
            "durationSeconds": min(duration, 7),
            "chapter": f"Сцена {card.scene_index:02d} / Кадр {card.shot_index:03d}",
            "title": title,
            "subtitle": subtitle,
        },
    }


def extract_stat_value(text: str) -> str:
    match = re.search(r"\d+\s*%?\+?", text)
    return match.group(0).replace(" ", "") if match else "95%+"


def has_stat_value(text: str) -> bool:
    return bool(re.search(r"(\d+\s*%|\$\s*\d+|\d+\s*(?:млн|миллион|миллиард|billion|million)|\d{4})", text, flags=re.IGNORECASE))


def short_title_from_description(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= 58:
        return text
    return text[:55].rsplit(" ", 1)[0] + "..."


def remotion_dedupe_key(job: dict) -> tuple[str, str]:
    props = job.get("props", {})
    template = str(props.get("template", ""))
    if template == "StatCard":
        return template, f"{props.get('value', '')}|{props.get('label', '')}"
    if template == "RerouteMap":
        return template, "single-reroute-animation"
    if template in {"FlowDiagram", "MapRoute", "MapSplit"}:
        return template, str(props.get("title", template))
    return template, f"{props.get('chapter', '')}|{props.get('title', '')}"


class ContentPipelineApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("Content Pipeline - Production Pack")
        self.root.geometry("1280x820")
        self.root.minsize(1060, 680)

        self.audio_path: Path | None = None
        self.output_base = Path.cwd() / "projects"
        self.current_project_dir: Path | None = None
        self.worker: threading.Thread | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        self.title_var = StringVar(value="Интернет на дне океана")
        self.audio_var = StringVar(value="Голосовая дорожка не выбрана")
        self.output_var = StringVar(value=str(self.output_base))
        self.project_var = StringVar(value="Проект еще не создан")
        self.lm_enabled = BooleanVar(value=False)
        self.lm_url = StringVar(value="http://127.0.0.1:1234/v1")
        self.lm_model = StringVar(value="local-model")
        self.lm_context = StringVar(value="32768")
        self.lm_auto_unload = BooleanVar(value=True)
        self.lm_unload_delay = StringVar(value="60")
        self.lm_unload_timer: threading.Timer | None = None
        self.comfy_url = StringVar(value="http://127.0.0.1:8188")
        self.image_model_preference = StringVar(value="Auto")
        self.whisper_cli_var = StringVar(value=find_default_tool("whisper-cli.exe") or find_default_tool("main.exe"))
        self.whisper_model_var = StringVar(value=find_default_model())
        self.ffmpeg_var = StringVar(value=find_default_tool("ffmpeg.exe") or find_default_tool("ffmpeg"))
        self.whisper_language_var = StringVar(value="ru")
        self.transcript_segments: list[TranscriptSegment] = []
        self.asset_cards: list[AssetCard] = []
        self.current_asset_index: int | None = None
        self.asset_board_status_var = StringVar(value="Доска кадров еще не создана")
        self.whisper_json_path: Path | None = None
        self.whisper_srt_path: Path | None = None
        self.status_var = StringVar(value="Готово")
        self.scene_status_var = StringVar(value="Сначала сделайте транскрибацию")

        self.load_settings()
        self.configure_theme()
        self.build_ui()
        self.apply_modern_skin()
        self.style_sidebar()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.auto_load_latest_project()
        self.poll_log_queue()

    def configure_theme(self):
        self.root.configure(bg=APP_BG)
        self.root.option_add("*Font", ("Segoe UI", 9))
        self.root.option_add("*Entry.Font", ("Segoe UI", 9))
        self.root.option_add("*Text.Font", ("Consolas", 10))
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox", fieldbackground=PANEL_BG, background=PANEL_BG, foreground=TEXT_FG)
        style.configure(
            "Asset.Treeview",
            background=PANEL_BG,
            foreground=TEXT_FG,
            fieldbackground=PANEL_BG,
            bordercolor=BORDER,
            rowheight=28,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Asset.Treeview.Heading",
            background="#eaf0f8",
            foreground=MUTED_FG,
            font=("Segoe UI", 9, "bold"),
            relief="flat",
        )
        style.map("Asset.Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", TEXT_FG)])

    def apply_modern_skin(self, widget=None):
        widget = widget or self.root
        for child in widget.winfo_children():
            cls = child.winfo_class()
            try:
                if cls == "Frame":
                    child.configure(bg=APP_BG)
                elif cls == "Label":
                    child.configure(bg=APP_BG, fg=TEXT_FG)
                elif cls == "Button":
                    child.configure(
                        bg=PANEL_BG,
                        fg=TEXT_FG,
                        activebackground="#e8f0ff",
                        activeforeground=TEXT_FG,
                        relief="flat",
                        bd=1,
                        padx=8,
                        pady=3,
                        highlightthickness=1,
                        highlightbackground=BORDER,
                    )
                elif cls == "Entry":
                    child.configure(bg=PANEL_BG, fg=TEXT_FG, insertbackground=TEXT_FG, relief="solid", bd=1)
                elif cls == "Listbox":
                    child.configure(
                        bg=PANEL_BG,
                        fg=TEXT_FG,
                        selectbackground="#dbeafe",
                        selectforeground=TEXT_FG,
                        relief="flat",
                        bd=0,
                        highlightthickness=1,
                        highlightbackground=BORDER,
                    )
                elif cls == "Text":
                    child.configure(
                        bg=PANEL_BG,
                        fg=TEXT_FG,
                        insertbackground=TEXT_FG,
                        relief="flat",
                        bd=0,
                        highlightthickness=1,
                        highlightbackground=BORDER,
                    )
            except Exception:
                pass
            self.apply_modern_skin(child)

    def style_sidebar(self):
        if not hasattr(self, "sidebar"):
            return
        self.sidebar.configure(bg=SIDEBAR_BG)
        for child in self.sidebar.winfo_children():
            try:
                if child is self.nav:
                    child.configure(
                        bg=SIDEBAR_BG,
                        fg=SIDEBAR_FG,
                        selectbackground=ACCENT,
                        selectforeground="#ffffff",
                        relief="flat",
                        bd=0,
                        highlightthickness=0,
                        activestyle="none",
                    )
                elif child.winfo_class() == "Label":
                    text = child.cget("text")
                    child.configure(bg=SIDEBAR_BG, fg="#ffffff" if text == "Content Pipeline" else "#8ea1bd")
            except Exception:
                pass

    def build_ui(self):
        shell = Frame(self.root, bg=APP_BG)
        shell.pack(fill=BOTH, expand=True)

        self.sidebar = Frame(shell, padx=12, pady=12, width=220, bg=SIDEBAR_BG)
        self.sidebar.pack(side=LEFT, fill=Y)
        self.sidebar.pack_propagate(False)
        Label(self.sidebar, text="Content Pipeline", anchor=W, bg=SIDEBAR_BG, fg="#ffffff", font=("Segoe UI", 12, "bold")).pack(fill=X)
        Label(self.sidebar, text="Production Pack", anchor=W, bg=SIDEBAR_BG, fg="#8ea1bd").pack(fill=X, pady=(2, 12))
        self.nav = Listbox(self.sidebar, height=8, exportselection=False)
        self.nav.configure(
            bg=SIDEBAR_BG,
            fg=SIDEBAR_FG,
            selectbackground=ACCENT,
            selectforeground="#ffffff",
            relief="flat",
            bd=0,
            highlightthickness=0,
            activestyle="none",
        )
        self.nav.pack(fill=BOTH, expand=True)
        for item in [
            "1. Транскрибация",
            "2. Сцены / LM Studio",
            "3. Схемы",
            "4. Видеоряд",
            "5. Доска кадров",
            "6. Настройки",
            "7. Экспорт",
        ]:
            self.nav.insert(END, item)
        self.nav.selection_set(0)
        self.nav.bind("<<ListboxSelect>>", self.on_nav_select)

        self.content = Frame(shell, padx=18, pady=16, bg=APP_BG)
        self.content.pack(side=LEFT, fill=BOTH, expand=True)
        self.section_frames: dict[int, Frame] = {}
        self.build_transcription_section()
        self.build_scene_section()
        self.build_schemes_section()
        self.build_visual_plan_section()
        self.build_asset_board_section()
        self.build_settings_section()
        self.build_export_section()
        self.show_section(0)

    def build_transcription_section(self):
        frame = Frame(self.content)
        self.section_frames[0] = frame

        top = Frame(frame)
        top.pack(fill=X)
        Label(top, text="Название проекта:", anchor=W).pack(side=LEFT)
        Entry(top, textvariable=self.title_var, width=38).pack(side=LEFT, padx=(8, 16))
        Button(top, text="Выбрать голос", command=self.choose_audio).pack(side=LEFT)
        Label(top, textvariable=self.audio_var, anchor=W).pack(side=LEFT, fill=X, expand=True, padx=(8, 0))

        output = Frame(frame, pady=8)
        output.pack(fill=X)
        Button(output, text="Папка projects", command=self.choose_output).pack(side=LEFT)
        Label(output, textvariable=self.output_var, anchor=W).pack(side=LEFT, fill=X, expand=True, padx=(8, 0))

        project = Frame(frame)
        project.pack(fill=X, pady=(0, 8))
        Label(project, text="Текущий проект:", anchor=W).pack(side=LEFT)
        Label(project, textvariable=self.project_var, anchor=W).pack(side=LEFT, fill=X, expand=True, padx=(8, 0))
        Button(project, text="Открыть проект", command=self.choose_project).pack(side=LEFT)

        whisper = Frame(frame, pady=4)
        whisper.pack(fill=X)
        Button(whisper, text="whisper-cli.exe", command=self.choose_whisper_cli).pack(side=LEFT)
        Entry(whisper, textvariable=self.whisper_cli_var, width=34).pack(side=LEFT, padx=(6, 10))
        Button(whisper, text="model .bin", command=self.choose_whisper_model).pack(side=LEFT)
        Entry(whisper, textvariable=self.whisper_model_var, width=34).pack(side=LEFT, padx=(6, 10))

        whisper2 = Frame(frame, pady=4)
        whisper2.pack(fill=X)
        Button(whisper2, text="ffmpeg.exe", command=self.choose_ffmpeg).pack(side=LEFT)
        Entry(whisper2, textvariable=self.ffmpeg_var, width=42).pack(side=LEFT, padx=(6, 10))
        Label(whisper2, text="Язык").pack(side=LEFT)
        Entry(whisper2, textvariable=self.whisper_language_var, width=6).pack(side=LEFT, padx=(6, 10))
        Button(whisper2, text="Транскрибировать голос", command=self.start_transcription).pack(side=LEFT)
        Label(whisper2, textvariable=self.status_var, anchor=W).pack(side=LEFT, fill=X, expand=True, padx=(12, 0))

        Label(frame, text="Транскрипт:", anchor=W).pack(fill=X, pady=(10, 0))
        self.transcription_text = ScrolledText(frame, wrap="word", height=17)
        self.transcription_text.pack(fill=BOTH, expand=True)

        bottom = Frame(frame)
        bottom.pack(fill=BOTH, pady=(10, 0))
        Label(bottom, text="Лог:", anchor=W).pack(fill=X)
        self.log = ScrolledText(bottom, wrap="word", height=8)
        self.log.pack(fill=BOTH, expand=True)

    def build_scene_section(self):
        frame = Frame(self.content)
        self.section_frames[1] = frame

        settings = Frame(frame)
        settings.pack(fill=X)
        Label(settings, text="LM Studio URL:").pack(side=LEFT)
        Entry(settings, textvariable=self.lm_url, width=38).pack(side=LEFT, padx=(6, 4))
        Button(settings, text="Вставить URL", command=self.paste_lm_url).pack(side=LEFT, padx=(0, 12))
        Label(settings, text="Model:").pack(side=LEFT)
        Entry(settings, textvariable=self.lm_model, width=34).pack(side=LEFT, padx=(6, 4))
        Button(settings, text="Вставить model", command=self.paste_lm_model).pack(side=LEFT, padx=(0, 12))
        Button(settings, text="Анализировать сцены", command=self.start_scene_analysis).pack(side=LEFT)

        load_settings = Frame(frame, pady=6)
        load_settings.pack(fill=X)
        Label(load_settings, text="Context Length:").pack(side=LEFT)
        Entry(load_settings, textvariable=self.lm_context, width=10).pack(side=LEFT, padx=(6, 10))
        Button(load_settings, text="Загрузить модель с этим context", command=self.start_lm_model_load).pack(side=LEFT)
        Button(load_settings, text="Сохранить настройки", command=self.save_settings).pack(side=LEFT, padx=(8, 0))

        status = Frame(frame, pady=8)
        status.pack(fill=X)
        Label(status, text="Проект:", anchor=W).pack(side=LEFT)
        Label(status, textvariable=self.project_var, anchor=W).pack(side=LEFT, fill=X, expand=True, padx=(8, 16))
        Label(status, textvariable=self.scene_status_var, anchor=W).pack(side=LEFT)

        Label(frame, text="Транскрипт для анализа:", anchor=W).pack(fill=X)
        self.script_text = ScrolledText(frame, wrap="word", height=12)
        self.script_text.pack(fill=BOTH, expand=True)

        scene_controls = Frame(frame, pady=8)
        scene_controls.pack(fill=X)
        Button(scene_controls, text="Загрузить .txt", width=18, command=self.load_text_file).pack(side=LEFT)
        Button(scene_controls, text="Вставить пример", width=18, command=self.insert_current_script).pack(side=LEFT, padx=(8, 0))
        Button(scene_controls, text="Очистить текст", width=18, command=lambda: self.script_text.delete("1.0", END)).pack(side=LEFT, padx=(8, 0))
        Button(scene_controls, text="Экспортировать монтажный пакет", width=28, command=self.start_generation).pack(side=LEFT, padx=(20, 0))

        Label(frame, text="Сцены / результат анализа:", anchor=W).pack(fill=X)
        self.scene_preview = ScrolledText(frame, wrap="word", height=10)
        self.scene_preview.pack(fill=BOTH, expand=True)

    def build_schemes_section(self):
        frame = Frame(self.content)
        self.section_frames[2] = frame

        top = Frame(frame)
        top.pack(fill=X)
        Label(top, text="Проект:", anchor=W).pack(side=LEFT)
        Label(top, textvariable=self.project_var, anchor=W).pack(side=LEFT, fill=X, expand=True, padx=(8, 16))
        Button(top, text="Создать базовые схемы", command=self.generate_sample_schemes).pack(side=LEFT)

        info = Frame(frame, pady=10)
        info.pack(fill=X)
        Label(
            info,
            text="Сейчас это первый слой: точные SVG-схемы из JSON. Позже сюда добавим генерацию схем по сценам через LM Studio.",
            anchor=W,
            wraplength=820,
            justify=LEFT,
        ).pack(fill=X)

        Label(frame, text="Созданные схемы:", anchor=W).pack(fill=X)
        self.schemes_preview = ScrolledText(frame, wrap="word", height=24)
        self.schemes_preview.pack(fill=BOTH, expand=True)

    def build_visual_plan_section(self):
        frame = Frame(self.content)
        self.section_frames[3] = frame

        top = Frame(frame)
        top.pack(fill=X)
        Label(top, text="Проект:", anchor=W).pack(side=LEFT)
        Label(top, textvariable=self.project_var, anchor=W).pack(side=LEFT, fill=X, expand=True, padx=(8, 16))
        Button(top, text="Создать список кадров через LM Studio", command=self.start_visual_plan).pack(side=LEFT)
        Button(top, text="Локальный черновик", command=lambda: self.start_visual_plan(use_lm=False)).pack(side=LEFT, padx=(8, 0))

        model_settings = Frame(frame, pady=6)
        model_settings.pack(fill=X)
        Label(model_settings, text="Image model preference:").pack(side=LEFT)
        ttk.Combobox(
            model_settings,
            textvariable=self.image_model_preference,
            values=["Auto", "Krea", "Ideogram"],
            state="readonly",
            width=12,
        ).pack(side=LEFT, padx=(8, 12))
        Button(model_settings, text="Сохранить настройки", command=self.save_settings).pack(side=LEFT)

        Label(
            frame,
            text="Доска кадров собирает весь продакшн в одном месте: сцены, кадры, маршрут генерации, промты, статусы и готовые outputs.",
            anchor=W,
            wraplength=900,
            justify=LEFT,
        ).pack(fill=X, pady=(10, 8))

        Label(frame, textvariable=self.asset_board_status_var, anchor=W).pack(fill=X, pady=(0, 8))

        Label(frame, text="План видеоряда:", anchor=W).pack(fill=X)
        self.visual_plan_preview = ScrolledText(frame, wrap="word", height=28)
        self.visual_plan_preview.pack(fill=BOTH, expand=True)

    def build_asset_board_section(self):
        frame = Frame(self.content)
        self.section_frames[4] = frame

        top = Frame(frame)
        top.pack(fill=X)
        Label(top, text="Проект:", anchor=W).pack(side=LEFT)
        Label(top, textvariable=self.project_var, anchor=W).pack(side=LEFT, fill=X, expand=True, padx=(8, 16))
        Button(top, text="Создать/обновить доску", command=self.create_asset_board).pack(side=LEFT)
        Button(top, text="Открыть outputs", command=self.open_asset_outputs).pack(side=LEFT, padx=(8, 0))

        comfy_row = Frame(frame, pady=6)
        comfy_row.pack(fill=X)
        Label(comfy_row, text="ComfyUI URL:", anchor=W).pack(side=LEFT)
        Entry(comfy_row, textvariable=self.comfy_url, width=34).pack(side=LEFT, padx=(8, 10))
        Button(comfy_row, text="Сохранить URL", command=self.save_settings).pack(side=LEFT)

        body = Frame(frame, pady=8)
        body.pack(fill=BOTH, expand=True)

        left = Frame(body)
        left.pack(side=LEFT, fill=BOTH, expand=False)
        Label(left, text="Сцены и кадры:", anchor=W, font=("Segoe UI", 10, "bold")).pack(fill=X, pady=(0, 6))
        tree_wrap = Frame(left, bg=PANEL_BG, highlightthickness=1, highlightbackground=BORDER)
        tree_wrap.pack(fill=BOTH, expand=True)
        self.asset_tree = ttk.Treeview(
            tree_wrap,
            columns=("status", "time", "route", "type"),
            show="tree headings",
            selectmode="browse",
            height=28,
            style="Asset.Treeview",
        )
        self.asset_tree.heading("#0", text="Кадр")
        self.asset_tree.heading("status", text="Status")
        self.asset_tree.heading("time", text="Time")
        self.asset_tree.heading("route", text="Route")
        self.asset_tree.heading("type", text="Type")
        self.asset_tree.column("#0", width=74, minwidth=70, stretch=False)
        self.asset_tree.column("status", width=82, minwidth=76, stretch=False)
        self.asset_tree.column("time", width=134, minwidth=118, stretch=False)
        self.asset_tree.column("route", width=104, minwidth=92, stretch=False)
        self.asset_tree.column("type", width=178, minwidth=140, stretch=True)
        tree_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.asset_tree.yview)
        self.asset_tree.configure(yscrollcommand=tree_scroll.set)
        self.asset_tree.pack(side=LEFT, fill=BOTH, expand=True)
        tree_scroll.pack(side=RIGHT, fill=Y)
        self.asset_tree.bind("<<TreeviewSelect>>", self.on_asset_select)
        self.asset_tree.tag_configure("planned", foreground=MUTED_FG)
        self.asset_tree.tag_configure("generating", foreground=ACCENT)
        self.asset_tree.tag_configure("generated", foreground="#16803a")
        self.asset_tree.tag_configure("needs_review", foreground="#a15c00")
        self.asset_tree.tag_configure("approved", foreground="#0f7a45")
        self.asset_tree.tag_configure("rejected", foreground="#b42318")
        self.asset_tree.tag_configure("skipped", foreground="#6b7280")
        self.asset_tree.tag_configure("scene", foreground=TEXT_FG, font=("Segoe UI", 9, "bold"))

        right = Frame(body, padx=12)
        right.pack(side=LEFT, fill=BOTH, expand=True)

        self.asset_header = StringVar(value="Выберите кадр")
        Label(right, textvariable=self.asset_header, anchor=W, font=("Segoe UI", 11, "bold")).pack(fill=X)

        row1 = Frame(right, pady=4)
        row1.pack(fill=X)
        Label(row1, text="Status").pack(side=LEFT)
        self.asset_status = StringVar(value="planned")
        ttk.Combobox(row1, textvariable=self.asset_status, values=["planned", "generating", "generated", "needs_review", "approved", "rejected", "skipped"], state="readonly", width=14).pack(side=LEFT, padx=(6, 14))
        Label(row1, text="Route").pack(side=LEFT)
        self.asset_route = StringVar(value="")
        ttk.Combobox(row1, textvariable=self.asset_route, values=["image+video", "image"], state="readonly", width=14).pack(side=LEFT, padx=(6, 14))
        Label(row1, text="Image").pack(side=LEFT)
        self.asset_image_model = StringVar(value="")
        ttk.Combobox(row1, textvariable=self.asset_image_model, values=["", "krea2_turbo", "ideogram4"], state="readonly", width=14).pack(side=LEFT, padx=(6, 14))
        Label(row1, text="Video").pack(side=LEFT)
        self.asset_video_model = StringVar(value="")
        ttk.Combobox(row1, textvariable=self.asset_video_model, values=["", "ltx2.3_i2v"], state="readonly", width=12).pack(side=LEFT, padx=(6, 0))

        row2 = Frame(right, pady=4)
        row2.pack(fill=X)
        Label(row2, text="Screen text").pack(side=LEFT)
        self.asset_screen_text = StringVar(value="")
        Entry(row2, textvariable=self.asset_screen_text).pack(side=LEFT, fill=X, expand=True, padx=(8, 0))

        Label(right, text="Description:", anchor=W).pack(fill=X, pady=(8, 0))
        self.asset_description = ScrolledText(right, wrap="word", height=4)
        self.asset_description.pack(fill=X)

        Label(right, text="Image prompt:", anchor=W).pack(fill=X, pady=(8, 0))
        self.asset_image_prompt = ScrolledText(right, wrap="word", height=5)
        self.asset_image_prompt.pack(fill=X)

        Label(right, text="Video prompt:", anchor=W).pack(fill=X, pady=(8, 0))
        self.asset_video_prompt = ScrolledText(right, wrap="word", height=4)
        self.asset_video_prompt.pack(fill=X)

        row3 = Frame(right, pady=6)
        row3.pack(fill=X)
        self.asset_outputs = StringVar(value="")
        Label(row3, textvariable=self.asset_outputs, anchor=W, wraplength=700, justify=LEFT).pack(side=LEFT, fill=X, expand=True)

        Label(right, text="Notes:", anchor=W).pack(fill=X)
        self.asset_notes = ScrolledText(right, wrap="word", height=3)
        self.asset_notes.pack(fill=X)

        actions = Frame(right, pady=8)
        actions.pack(fill=X)
        Button(actions, text="Сохранить карточку", command=self.save_current_asset_card).pack(side=LEFT)
        Button(actions, text="Approve", command=lambda: self.set_current_asset_status("approved")).pack(side=LEFT, padx=(8, 0))
        Button(actions, text="Skip", command=lambda: self.set_current_asset_status("skipped")).pack(side=LEFT, padx=(8, 0))
        Button(actions, text="Needs fix", command=lambda: self.set_current_asset_status("needs_review")).pack(side=LEFT, padx=(8, 0))
        Button(actions, text="Открыть output", command=self.open_selected_asset_output).pack(side=LEFT, padx=(8, 0))

        generate_actions = Frame(right, pady=2)
        generate_actions.pack(fill=X)
        Button(generate_actions, text="Generate AI video", command=self.start_generate_selected_route).pack(side=LEFT)
        Button(generate_actions, text="Generate image", command=self.start_generate_selected_image).pack(side=LEFT, padx=(8, 0))
        Button(generate_actions, text="Generate LTX video", command=self.start_generate_selected_video).pack(side=LEFT, padx=(8, 0))

    def build_remotion_section(self):
        frame = Frame(self.content)
        self.section_frames[5] = frame

        top = Frame(frame)
        top.pack(fill=X)
        Label(top, text="Проект:", anchor=W).pack(side=LEFT)
        Label(top, textvariable=self.project_var, anchor=W).pack(side=LEFT, fill=X, expand=True, padx=(8, 16))
        Button(top, text="Создать Remotion jobs", command=self.create_remotion_jobs).pack(side=LEFT)
        Button(top, text="Рендерить jobs", command=self.start_remotion_render).pack(side=LEFT, padx=(8, 0))
        Button(top, text="Очистить renders", command=self.clean_remotion_renders).pack(side=LEFT, padx=(8, 0))

        Label(
            frame,
            text="Remotion рендерит motion graphics из JSON-заданий: stat cards, flow diagrams, chapter titles. Перед рендером нужны npm install в remotion_graphics.",
            anchor=W,
            wraplength=900,
            justify=LEFT,
        ).pack(fill=X, pady=(10, 8))

        Label(frame, text="Remotion jobs / render log:", anchor=W).pack(fill=X)
        self.remotion_preview = ScrolledText(frame, wrap="word", height=28)
        self.remotion_preview.pack(fill=BOTH, expand=True)

    def build_settings_section(self):
        frame = Frame(self.content)
        self.section_frames[5] = frame

        Label(frame, text="Настройки", anchor=W, font=("Segoe UI", 14, "bold")).pack(fill=X)
        Label(
            frame,
            text="Управление локальными сервисами и памятью. Авто-выгрузка LM Studio полезна перед ComfyUI/LTX, чтобы освободить видеопамять.",
            anchor=W,
            wraplength=900,
            justify=LEFT,
        ).pack(fill=X, pady=(6, 18))

        lm_panel = Frame(frame, padx=12, pady=12, highlightthickness=1, highlightbackground=BORDER)
        lm_panel.pack(fill=X, pady=(0, 12))
        Label(lm_panel, text="LM Studio", anchor=W, font=("Segoe UI", 11, "bold")).pack(fill=X)

        row1 = Frame(lm_panel, pady=6)
        row1.pack(fill=X)
        Label(row1, text="URL:", width=16, anchor=W).pack(side=LEFT)
        Entry(row1, textvariable=self.lm_url, width=44).pack(side=LEFT, padx=(0, 12))
        Label(row1, text="Model:", anchor=W).pack(side=LEFT)
        Entry(row1, textvariable=self.lm_model, width=42).pack(side=LEFT, padx=(8, 0))

        row2 = Frame(lm_panel, pady=6)
        row2.pack(fill=X)
        Label(row2, text="Context Length:", width=16, anchor=W).pack(side=LEFT)
        Entry(row2, textvariable=self.lm_context, width=12).pack(side=LEFT, padx=(0, 16))
        Checkbutton(row2, text="Авто-выгрузка после LM-задачи", variable=self.lm_auto_unload).pack(side=LEFT, padx=(0, 16))
        Label(row2, text="Задержка, сек:").pack(side=LEFT)
        Entry(row2, textvariable=self.lm_unload_delay, width=8).pack(side=LEFT, padx=(8, 0))

        row3 = Frame(lm_panel, pady=8)
        row3.pack(fill=X)
        Button(row3, text="Сохранить настройки", command=self.save_settings).pack(side=LEFT)
        Button(row3, text="Загрузить модель", command=self.start_lm_model_load).pack(side=LEFT, padx=(8, 0))
        Button(row3, text="Выгрузить модель сейчас", command=self.start_lm_model_unload).pack(side=LEFT, padx=(8, 0))
        Button(row3, text="Отменить таймер выгрузки", command=self.cancel_lm_unload_timer).pack(side=LEFT, padx=(8, 0))

        self.lm_unload_status_var = StringVar(value="Таймер выгрузки не активен")
        Label(lm_panel, textvariable=self.lm_unload_status_var, anchor=W).pack(fill=X, pady=(4, 0))

        comfy_panel = Frame(frame, padx=12, pady=12, highlightthickness=1, highlightbackground=BORDER)
        comfy_panel.pack(fill=X)
        Label(comfy_panel, text="ComfyUI", anchor=W, font=("Segoe UI", 11, "bold")).pack(fill=X)
        comfy_row = Frame(comfy_panel, pady=6)
        comfy_row.pack(fill=X)
        Label(comfy_row, text="URL:", width=16, anchor=W).pack(side=LEFT)
        Entry(comfy_row, textvariable=self.comfy_url, width=44).pack(side=LEFT, padx=(0, 12))
        Button(comfy_row, text="Сохранить настройки", command=self.save_settings).pack(side=LEFT)

    def build_export_section(self):
        frame = Frame(self.content)
        self.section_frames[6] = frame
        Label(frame, text="Экспорт", anchor=W, font=("Segoe UI", 14, "bold")).pack(fill=X)
        Label(
            frame,
            text="Финальная сборка ролика будет добавлена позже: таймлайн, аудио, сгенерированные assets, субтитры и экспорт.",
            anchor=W,
            wraplength=900,
            justify=LEFT,
        ).pack(fill=X, pady=(8, 0))

    def on_nav_select(self, _event=None):
        selection = self.nav.curselection()
        if selection:
            self.show_section(selection[0])

    def show_section(self, index: int):
        for frame in self.section_frames.values():
            frame.pack_forget()
        frame = self.section_frames.get(index)
        if frame:
            frame.pack(fill=BOTH, expand=True)

    def load_settings(self):
        if not SETTINGS_PATH.exists():
            return
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        self.lm_url.set(settings.get("lm_url", self.lm_url.get()))
        self.lm_model.set(settings.get("lm_model", self.lm_model.get()))
        self.lm_context.set(str(settings.get("lm_context", self.lm_context.get())))
        self.lm_auto_unload.set(bool(settings.get("lm_auto_unload", self.lm_auto_unload.get())))
        self.lm_unload_delay.set(str(settings.get("lm_unload_delay", self.lm_unload_delay.get())))
        self.comfy_url.set(settings.get("comfy_url", self.comfy_url.get()))
        self.image_model_preference.set(settings.get("image_model_preference", self.image_model_preference.get()))
        self.whisper_cli_var.set(settings.get("whisper_cli", self.whisper_cli_var.get()))
        self.whisper_model_var.set(settings.get("whisper_model", self.whisper_model_var.get()))
        self.ffmpeg_var.set(settings.get("ffmpeg", self.ffmpeg_var.get()))
        self.whisper_language_var.set(settings.get("whisper_language", self.whisper_language_var.get()))
        output_base = settings.get("output_base")
        if output_base:
            self.output_base = Path(output_base)
            self.output_var.set(str(self.output_base))

    def save_settings(self):
        settings = {
            "lm_url": self.lm_url.get().strip(),
            "lm_model": self.lm_model.get().strip(),
            "lm_context": self.lm_context.get().strip(),
            "lm_auto_unload": self.lm_auto_unload.get(),
            "lm_unload_delay": self.lm_unload_delay.get().strip(),
            "comfy_url": self.comfy_url.get().strip(),
            "image_model_preference": self.image_model_preference.get().strip(),
            "whisper_cli": self.whisper_cli_var.get().strip(),
            "whisper_model": self.whisper_model_var.get().strip(),
            "ffmpeg": self.ffmpeg_var.get().strip(),
            "whisper_language": self.whisper_language_var.get().strip(),
            "output_base": str(self.output_base),
        }
        SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        self.scene_status_var.set("Настройки сохранены")

    def on_close(self):
        self.cancel_lm_unload_timer(update_status=False)
        self.save_settings()
        self.root.destroy()

    def lm_ttl_seconds(self) -> int | None:
        try:
            value = int(self.lm_unload_delay.get().strip())
        except ValueError:
            return None
        return max(0, value)

    def cancel_lm_unload_timer(self, update_status: bool = True):
        if self.lm_unload_timer:
            self.lm_unload_timer.cancel()
            self.lm_unload_timer = None
        if update_status and hasattr(self, "lm_unload_status_var"):
            self.lm_unload_status_var.set("Таймер выгрузки отменен")

    def schedule_lm_unload(self, reason: str):
        if not self.lm_auto_unload.get():
            if hasattr(self, "lm_unload_status_var"):
                self.lm_unload_status_var.set("Авто-выгрузка выключена")
            return
        delay = self.lm_ttl_seconds()
        if delay is None:
            self.log_queue.put("__SCENE_STATUS__Неверная задержка выгрузки LM Studio")
            return
        self.cancel_lm_unload_timer(update_status=False)
        if delay <= 0:
            self.log_queue.put(f"LM Studio unload now: {reason}")
            unload_thread = threading.Thread(target=self.unload_lm_model_worker, args=(reason,), daemon=True)
            unload_thread.start()
            return
        self.lm_unload_timer = threading.Timer(delay, self.unload_lm_model_timer_worker, args=(reason,))
        self.lm_unload_timer.daemon = True
        self.lm_unload_timer.start()
        status = f"LM Studio будет выгружена через {delay} сек. Причина: {reason}"
        if hasattr(self, "lm_unload_status_var"):
            self.lm_unload_status_var.set(status)
        self.log_queue.put(status)

    def unload_lm_model_timer_worker(self, reason: str):
        self.lm_unload_timer = None
        self.unload_lm_model_worker(reason)

    def start_lm_model_unload(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Задача уже идет", "Дождитесь завершения текущей задачи.")
            return
        self.save_settings()
        self.cancel_lm_unload_timer(update_status=False)
        self.worker = threading.Thread(target=self.unload_lm_model_worker, args=("manual",), daemon=True)
        self.worker.start()

    def unload_lm_model_worker(self, reason: str):
        try:
            model = self.lm_model.get().strip()
            if not model:
                raise ValueError("Не указан model id")
            self.log_queue.put(f"LM Studio unload: model={model}, reason={reason}")
            result = unload_lm_studio_model(self.lm_url.get(), model)
            self.log_queue.put(f"LM Studio unload result: {json.dumps(result, ensure_ascii=False)}")
            self.log_queue.put("__LM_UNLOAD_STATUS__Модель выгружена из памяти")
        except Exception as exc:
            self.log_queue.put(f"ERROR: LM Studio unload failed: {explain_lm_studio_error(exc)}")
            self.log_queue.put("__LM_UNLOAD_STATUS__Ошибка выгрузки LM Studio")

    def auto_load_latest_project(self):
        if not self.output_base.exists():
            return
        candidates = [
            path
            for path in self.output_base.iterdir()
            if path.is_dir() and (path / "01_transcript_segments.json").exists()
        ]
        if not candidates:
            return
        latest = max(candidates, key=lambda path: (path / "01_transcript_segments.json").stat().st_mtime)
        try:
            self.load_project(latest)
        except Exception as exc:
            self.status_var.set(f"Не удалось открыть последний проект: {exc}")

    def choose_project(self):
        folder = filedialog.askdirectory(initialdir=str(self.output_base))
        if not folder:
            return
        try:
            self.load_project(Path(folder))
        except Exception as exc:
            messagebox.showerror("Не удалось открыть проект", str(exc))

    def load_project(self, project_dir: Path):
        project_dir = project_dir.resolve()
        segments_path = project_dir / "01_transcript_segments.json"
        transcript_path = project_dir / "01_transcript.txt"
        if not segments_path.exists():
            raise FileNotFoundError(f"Не найден файл: {segments_path}")
        raw_segments = json.loads(segments_path.read_text(encoding="utf-8"))
        self.transcript_segments = [TranscriptSegment(**item) for item in raw_segments]
        transcript_text = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else "\n".join(
            segment.text for segment in self.transcript_segments
        )

        self.current_project_dir = project_dir
        self.project_var.set(str(project_dir))
        self.title_var.set(project_dir.name)
        self.output_base = project_dir.parent
        self.output_var.set(str(self.output_base))
        self.whisper_json_path = project_dir / "01_whisper_raw.json" if (project_dir / "01_whisper_raw.json").exists() else None
        self.whisper_srt_path = project_dir / "04_subtitles_whisper.srt" if (project_dir / "04_subtitles_whisper.srt").exists() else None

        voice_dir = project_dir / "voice"
        audio_files = []
        if voice_dir.exists():
            audio_files = [path for path in voice_dir.iterdir() if path.suffix.lower() in AUDIO_EXTENSIONS]
        if audio_files:
            self.audio_path = audio_files[0]
            self.audio_var.set(str(self.audio_path))
        else:
            self.audio_path = None
            self.audio_var.set("Голосовая дорожка не выбрана")

        self.transcription_text.delete("1.0", END)
        self.transcription_text.insert(END, transcript_text)
        self.script_text.delete("1.0", END)
        self.script_text.insert(END, transcript_text)
        self.status_var.set("Проект загружен")
        self.scene_status_var.set("Готово к анализу сцен")

        scenes_path = project_dir / "02_scenes.json"
        if scenes_path.exists():
            scenes = [Scene(**item) for item in json.loads(scenes_path.read_text(encoding="utf-8"))]
            preview = []
            for scene in scenes:
                preview.append(f"{scene.index:02d}. {scene.start}-{scene.end} | {scene.title}")
            self.scene_preview.delete("1.0", END)
            self.scene_preview.insert(END, "\n".join(preview))

        visual_path = project_dir / "04_shot_list.json"
        if visual_path.exists():
            shots = [Shot(**item) for item in json.loads(visual_path.read_text(encoding="utf-8"))]
            self.visual_plan_preview.delete("1.0", END)
            self.visual_plan_preview.insert(
                END,
                "\n".join(
                    f"{shot.index:03d}. {shot.start}-{shot.end} | сцена {shot.scene_index} | {shot.visual_type} | {shot.description}"
                    for shot in shots
                ),
            )

        asset_path = project_dir / "06_asset_board.json"
        if asset_path.exists():
            self.load_asset_board_into_ui()
        else:
            self.asset_cards = []
            self.populate_asset_board()

    def choose_audio(self):
        filename = filedialog.askopenfilename(
            initialdir=str(Path.cwd()),
            filetypes=[("Audio", "*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus"), ("All files", "*.*")],
        )
        if not filename:
            return
        path = Path(filename)
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            messagebox.showwarning("Проверь файл", "Это не похоже на аудиофайл, но его можно использовать как вложение.")
        self.audio_path = path
        self.audio_var.set(str(path))

    def choose_whisper_cli(self):
        filename = filedialog.askopenfilename(
            initialdir=str(Path.cwd()),
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if filename:
            self.whisper_cli_var.set(filename)

    def choose_whisper_model(self):
        filename = filedialog.askopenfilename(
            initialdir=str(Path.cwd()),
            filetypes=[("GGML model", "*.bin"), ("All files", "*.*")],
        )
        if filename:
            self.whisper_model_var.set(filename)

    def choose_ffmpeg(self):
        filename = filedialog.askopenfilename(
            initialdir=str(Path.cwd()),
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if filename:
            self.ffmpeg_var.set(filename)

    def paste_lm_url(self):
        try:
            value = self.root.clipboard_get().strip()
        except Exception:
            messagebox.showerror("Буфер пуст", "Не удалось прочитать буфер обмена.")
            return
        if not value:
            return
        value = value.rstrip("/")
        if not value.endswith("/v1"):
            value += "/v1"
        self.lm_url.set(value)
        self.save_settings()

    def paste_lm_model(self):
        try:
            value = self.root.clipboard_get().strip()
        except Exception:
            messagebox.showerror("Буфер пуст", "Не удалось прочитать буфер обмена.")
            return
        if not value:
            return
        # LM Studio sometimes copies surrounding labels or whitespace; keep the likely model id.
        value = value.replace("API Model Identifier", "").strip()
        value = value.splitlines()[0].strip()
        self.lm_model.set(value)
        self.save_settings()

    def start_lm_model_load(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.lm_url.get().strip() or not self.lm_model.get().strip():
            messagebox.showerror("Нет настроек LM Studio", "Укажите URL и model id.")
            return
        try:
            context_length = int(self.lm_context.get().strip())
        except ValueError:
            messagebox.showerror("Неверный context", "Context Length должен быть числом, например 32768.")
            return
        self.save_settings()
        self.cancel_lm_unload_timer(update_status=False)
        self.scene_status_var.set("Загружаю модель...")
        self.worker = threading.Thread(target=self.load_lm_model_worker, args=(context_length,), daemon=True)
        self.worker.start()

    def load_lm_model_worker(self, context_length: int):
        try:
            self.log_queue.put(
                f"LM Studio load: model={self.lm_model.get().strip()}, context_length={context_length}, "
                f"endpoint={lm_openai_to_rest_base(self.lm_url.get())}/models/load"
            )
            result = load_lm_studio_model(self.lm_url.get(), self.lm_model.get(), context_length)
            load_config = result.get("load_config", {})
            actual_context = load_config.get("context_length", "unknown")
            self.log_queue.put(f"LM Studio load result: {json.dumps(result, ensure_ascii=False)}")
            self.log_queue.put(f"__SCENE_STATUS__Модель загружена, context={actual_context}")
        except Exception as exc:
            self.log_queue.put(f"ERROR: {explain_lm_studio_error(exc)}")
            self.log_queue.put("__SCENE_STATUS__Ошибка загрузки модели")

    def choose_output(self):
        folder = filedialog.askdirectory(initialdir=str(self.output_base))
        if folder:
            self.output_base = Path(folder)
            self.output_var.set(str(self.output_base))
            self.save_settings()

    def load_text_file(self):
        filename = filedialog.askopenfilename(
            initialdir=str(Path.cwd()),
            filetypes=[("Text", "*.txt *.md"), ("All files", "*.*")],
        )
        if not filename:
            return
        text = Path(filename).read_text(encoding="utf-8")
        self.script_text.delete("1.0", END)
        self.script_text.insert(END, text)

    def insert_current_script(self):
        path = Path.cwd() / "интернет-на-дне-дикторский-текст.txt"
        if not path.exists():
            messagebox.showerror("Файл не найден", f"Не найден файл: {path}")
            return
        self.script_text.delete("1.0", END)
        self.script_text.insert(END, path.read_text(encoding="utf-8"))

    def start_transcription(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.audio_path:
            messagebox.showerror("Нет голоса", "Сначала выберите голосовую дорожку.")
            return
        missing = []
        if not self.whisper_cli_var.get().strip():
            missing.append("whisper-cli.exe")
        if not self.whisper_model_var.get().strip():
            missing.append("model .bin")
        if not self.ffmpeg_var.get().strip():
            missing.append("ffmpeg.exe")
        if missing:
            messagebox.showerror("Не хватает путей", "Укажите: " + ", ".join(missing))
            return

        self.status_var.set("Транскрибирую...")
        self.log.delete("1.0", END)
        self.worker = threading.Thread(target=self.transcribe_audio, daemon=True)
        self.worker.start()

    def transcribe_audio(self):
        try:
            title = self.title_var.get().strip() or "transcription"
            project_dir = self.output_base / slugify(title)
            self.current_project_dir = project_dir
            self.log_queue.put("__PROJECT__" + str(project_dir))
            work_dir = project_dir / "transcription"
            self.log_queue.put("Stage 1/3: checking whisper.cpp paths")
            transcript_text, segments, json_path, srt_path = transcribe_with_whisper_cpp(
                audio_path=self.audio_path,
                whisper_cli=Path(self.whisper_cli_var.get().strip()),
                model_path=Path(self.whisper_model_var.get().strip()),
                ffmpeg_path=Path(self.ffmpeg_var.get().strip()),
                language=self.whisper_language_var.get().strip() or "ru",
                output_dir=work_dir,
                log_callback=self.log_queue.put,
            )
            self.transcript_segments = segments
            self.whisper_json_path = json_path
            self.whisper_srt_path = srt_path
            if not transcript_text and segments:
                transcript_text = "\n".join(segment.text for segment in segments)
            self.log_queue.put(f"Stage 2/3: got {len(segments)} segment(s)")
            write_transcription_project_files(
                project_dir=project_dir,
                audio_path=self.audio_path,
                transcript_text=transcript_text,
                transcript_segments=segments,
                whisper_json_path=json_path,
                whisper_srt_path=srt_path,
            )
            self.log_queue.put("__SCRIPT__" + transcript_text)
            self.log_queue.put("Stage 3/3: transcript saved as project starting point")
            self.log_queue.put("__STATUS__Транскрипт готов")
            self.log_queue.put("__SCENE_STATUS__Готово к анализу сцен")
        except Exception as exc:
            self.log_queue.put(f"ERROR: {exc}")
            self.log_queue.put("__STATUS__Ошибка транскрибации")

    def start_generation(self):
        if self.worker and self.worker.is_alive():
            return
        title = self.title_var.get().strip()
        script_text = self.script_text.get("1.0", END).strip()
        if not title:
            messagebox.showerror("Нет названия", "Введите название проекта.")
            return
        if not script_text:
            messagebox.showerror("Нет текста", "Загрузите или вставьте текст диктора.")
            return

        self.status_var.set("Создаю пакет...")
        self.log.delete("1.0", END)
        self.worker = threading.Thread(target=self.generate_pack, args=(title, script_text), daemon=True)
        self.worker.start()

    def start_scene_analysis(self):
        if self.worker and self.worker.is_alive():
            return
        self.save_settings()
        if not self.current_project_dir:
            guessed = self.output_base / slugify(self.title_var.get().strip() or "transcription")
            if guessed.exists():
                self.current_project_dir = guessed
                self.project_var.set(str(guessed))
        if not self.current_project_dir:
            messagebox.showerror("Нет проекта", "Сначала сделайте транскрибацию в первом разделе.")
            return
        if not self.transcript_segments:
            segments_path = self.current_project_dir / "01_transcript_segments.json"
            if segments_path.exists():
                raw_segments = json.loads(segments_path.read_text(encoding="utf-8"))
                self.transcript_segments = [TranscriptSegment(**item) for item in raw_segments]
        if not self.transcript_segments:
            messagebox.showerror("Нет сегментов", "Не найден transcript_segments.json. Сначала выполните транскрибацию.")
            return

        self.scene_status_var.set("Анализирую...")
        self.scene_preview.delete("1.0", END)
        self.worker = threading.Thread(target=self.analyze_scenes, daemon=True)
        self.worker.start()

    def generate_sample_schemes(self):
        if not self.current_project_dir:
            guessed = self.output_base / slugify(self.title_var.get().strip() or "transcription")
            if guessed.exists():
                self.current_project_dir = guessed
                self.project_var.set(str(guessed))
        if not self.current_project_dir:
            messagebox.showerror("Нет проекта", "Сначала создайте проект через транскрибацию.")
            return
        schemes_dir = self.current_project_dir / "schemes"
        try:
            schema_generator.write_samples(schemes_dir)
            files = sorted(path.name for path in schemes_dir.glob("*") if path.is_file())
            self.schemes_preview.delete("1.0", END)
            self.schemes_preview.insert(END, f"Папка: {schemes_dir}\n\n")
            self.schemes_preview.insert(END, "\n".join(files))
            self.scene_status_var.set("Базовые схемы созданы")
        except Exception as exc:
            messagebox.showerror("Ошибка схем", str(exc))

    def start_visual_plan(self, use_lm: bool = True):
        if self.worker and self.worker.is_alive():
            return
        self.save_settings()
        if not self.current_project_dir:
            guessed = self.output_base / slugify(self.title_var.get().strip() or "transcription")
            if guessed.exists():
                self.current_project_dir = guessed
                self.project_var.set(str(guessed))
        if not self.current_project_dir:
            messagebox.showerror("Нет проекта", "Сначала создайте проект через транскрибацию.")
            return
        scenes_path = self.current_project_dir / "02_scenes.json"
        if not scenes_path.exists():
            messagebox.showerror("Нет сцен", "Сначала выполните этап 'Сцены / LM Studio'.")
            return
        raw_scenes = json.loads(scenes_path.read_text(encoding="utf-8"))
        scenes = [Scene(**item) for item in raw_scenes]
        self.visual_plan_preview.delete("1.0", END)
        self.worker = threading.Thread(target=self.generate_visual_plan, args=(scenes, use_lm), daemon=True)
        self.worker.start()

    def create_asset_board(self):
        if not self.current_project_dir:
            guessed = self.output_base / slugify(self.title_var.get().strip() or "transcription")
            if guessed.exists():
                self.current_project_dir = guessed
                self.project_var.set(str(guessed))
        if not self.current_project_dir:
            messagebox.showerror("Нет проекта", "Сначала создайте проект через транскрибацию.")
            return
        try:
            self.asset_cards = build_asset_board(self.current_project_dir)
            write_asset_board(self.current_project_dir, self.asset_cards)
            self.populate_asset_board()
            self.asset_board_status_var.set(f"Доска обновлена: {len(self.asset_cards)} кадров")
        except Exception as exc:
            messagebox.showerror("Ошибка доски кадров", str(exc))

    def load_asset_board_into_ui(self):
        if not self.current_project_dir:
            return
        self.asset_cards = load_asset_board(self.current_project_dir)
        self.populate_asset_board()

    def populate_asset_board(self):
        for item in self.asset_tree.get_children():
            self.asset_tree.delete(item)
        status_counts = {
            "planned": 0,
            "generating": 0,
            "generated": 0,
            "needs_review": 0,
            "approved": 0,
            "rejected": 0,
            "skipped": 0,
        }
        current_scene = None
        current_scene_iid = ""
        for card_index, card in enumerate(self.asset_cards):
            status_counts[card.status] = status_counts.get(card.status, 0) + 1
            if card.scene_index != current_scene:
                current_scene = card.scene_index
                current_scene_iid = f"scene_{card.scene_index:03d}"
                self.asset_tree.insert(
                    "",
                    END,
                    iid=current_scene_iid,
                    text=f"Scene {card.scene_index:02d}",
                    values=("", "", "", card.scene_title),
                    open=True,
                    tags=("scene",),
                )
            status_mark = {
                "planned": "Planned",
                "generating": "Generating",
                "generated": "Generated",
                "needs_review": "Review",
                "approved": "Approved",
                "rejected": "Rejected",
                "skipped": "Skipped",
            }.get(card.status, "Planned")
            self.asset_tree.insert(
                current_scene_iid,
                END,
                iid=f"card_{card_index}_{card.shot_index:03d}",
                text=f"{card.shot_index:03d}",
                values=(status_mark, f"{card.start}-{card.end}", card.route, card.visual_type),
                tags=(card.status,),
            )
        if not self.asset_cards:
            self.asset_board_status_var.set("Доска кадров еще не создана")
            self.asset_header.set("Выберите кадр")
            self.asset_outputs.set("")
            return
        self.asset_board_status_var.set(
            " | ".join(
                [
                    f"Всего: {len(self.asset_cards)}",
                    f"в работе: {status_counts.get('generating', 0)}",
                    f"готово: {status_counts.get('generated', 0)}",
                    f"на проверке: {status_counts.get('needs_review', 0)}",
                    f"утверждено: {status_counts.get('approved', 0)}",
                    f"пропущено: {status_counts.get('skipped', 0)}",
                ]
            )
        )

    def selected_asset_card(self) -> AssetCard | None:
        selection = self.asset_tree.selection()
        if not selection:
            return None
        item_id = selection[0]
        if not item_id.startswith("card_"):
            return None
        try:
            card_index = int(item_id.split("_", 2)[1])
        except (IndexError, ValueError):
            return None
        if 0 <= card_index < len(self.asset_cards):
            self.current_asset_index = card_index
            return self.asset_cards[card_index]
        return None

    def on_asset_select(self, _event=None):
        card = self.selected_asset_card()
        if not card:
            return
        self.asset_header.set(
            f"Кадр {card.shot_index:03d} | Сцена {card.scene_index} | {card.start}-{card.end} | {card.route}"
        )
        self.asset_status.set(card.status)
        self.asset_route.set(card.route)
        self.asset_image_model.set(card.image_model)
        self.asset_video_model.set(card.video_model)
        self.asset_screen_text.set(card.screen_text)
        self.replace_text_widget(self.asset_description, card.description)
        self.replace_text_widget(self.asset_image_prompt, card.image_prompt)
        self.replace_text_widget(self.asset_video_prompt, card.video_prompt)
        self.replace_text_widget(self.asset_notes, card.notes)
        outputs = []
        if card.image_output:
            outputs.append(f"Image: {card.image_output}")
        if card.video_output:
            outputs.append(f"Video: {card.video_output}")
        if not outputs:
            outputs.append("Outputs: not generated yet")
        self.asset_outputs.set("\n".join(outputs))

    def replace_text_widget(self, widget: ScrolledText, text: str):
        widget.delete("1.0", END)
        widget.insert(END, text or "")
        widget.yview_moveto(0)

    def save_current_asset_card(self):
        if self.current_asset_index is None or not self.current_project_dir:
            return
        card = self.asset_cards[self.current_asset_index]
        card.status = self.asset_status.get()
        card.route = self.asset_route.get()
        card.image_model = self.asset_image_model.get()
        card.video_model = self.asset_video_model.get()
        card.screen_text = self.asset_screen_text.get()
        card.description = self.asset_description.get("1.0", END).strip()
        card.image_prompt = self.asset_image_prompt.get("1.0", END).strip()
        card.video_prompt = self.asset_video_prompt.get("1.0", END).strip()
        card.notes = self.asset_notes.get("1.0", END).strip()
        card.approved = card.status == "approved"
        write_asset_board(self.current_project_dir, self.asset_cards)
        saved_index = self.current_asset_index
        self.populate_asset_board()
        self.select_asset_card(saved_index)

    def set_current_asset_status(self, status: str):
        self.asset_status.set(status)
        self.save_current_asset_card()

    def open_asset_outputs(self):
        if not self.current_project_dir:
            return
        output_dir = self.current_project_dir / "renders"
        output_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(output_dir)

    def open_selected_asset_output(self):
        if self.current_asset_index is None:
            return
        card = self.asset_cards[self.current_asset_index]
        for value in [card.video_output, card.image_output]:
            if value and Path(value).exists():
                os.startfile(value)
                return
        if self.current_project_dir:
            self.open_asset_outputs()

    def tree_item_for_asset_card(self, card_index: int | None) -> str | None:
        if card_index is None:
            return None
        prefix = f"card_{card_index}_"
        for scene_id in self.asset_tree.get_children():
            for item_id in self.asset_tree.get_children(scene_id):
                if item_id.startswith(prefix):
                    return item_id
        return None

    def select_asset_card(self, card_index: int | None):
        item_id = self.tree_item_for_asset_card(card_index)
        if not item_id:
            return
        parent = self.asset_tree.parent(item_id)
        if parent:
            self.asset_tree.item(parent, open=True)
        self.asset_tree.selection_set(item_id)
        self.asset_tree.focus(item_id)
        self.asset_tree.see(item_id)
        self.current_asset_index = card_index
        self.on_asset_select()

    def start_generate_selected_route(self):
        self.start_asset_generation("route")

    def start_generate_selected_image(self):
        self.start_asset_generation("image")

    def start_generate_selected_graphic(self):
        self.start_asset_generation("graphic")

    def start_generate_selected_video(self):
        self.start_asset_generation("video")

    def start_asset_generation(self, mode: str):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Задача уже идет", "Дождитесь завершения текущей генерации.")
            return
        if self.current_asset_index is None or not self.current_project_dir:
            messagebox.showerror("Нет карточки", "Выберите кадр на доске кадров.")
            return
        self.save_settings()
        self.save_current_asset_card()
        card_index = self.current_asset_index
        self.worker = threading.Thread(target=self.generate_asset_worker, args=(card_index, mode), daemon=True)
        self.worker.start()

    def generate_asset_worker(self, card_index: int, mode: str):
        try:
            assert self.current_project_dir is not None
            project_dir = self.current_project_dir
            cards = load_asset_board(project_dir)
            card = cards[card_index]
            card.status = "generating"
            write_asset_board(project_dir, cards)
            self.log_queue.put(f"__ASSET_STATUS__Генерация кадра {card.shot_index:03d} via {mode}")
            self.log_queue.put("__ASSET_BOARD_REFRESH__")

            if mode in {"route", "graphic"}:
                card.route = "image+video"
                card.visual_type = "ai_image_then_i2v"
                card.remotion_template = ""
                card.remotion_output = ""
                if not card.image_model:
                    card.image_model = preferred_realistic_image_model(self.image_model_preference.get())
                if not card.video_model:
                    card.video_model = "ltx2.3_i2v"
                generated_any = False
                if card.image_model:
                    image_path = generate_comfy_image(self.comfy_url.get(), project_dir, card)
                    card.image_output = str(image_path)
                    generated_any = True
                if card.video_model:
                    video_path = generate_comfy_ltx_video(self.comfy_url.get(), project_dir, card)
                    card.video_output = str(video_path)
                    generated_any = True
                if not generated_any:
                    raise RuntimeError("AI video generation needs image and video models.")
            elif mode == "image":
                image_path = generate_comfy_image(self.comfy_url.get(), project_dir, card)
                card.image_output = str(image_path)
            elif mode == "video":
                video_path = generate_comfy_ltx_video(self.comfy_url.get(), project_dir, card)
                card.video_output = str(video_path)
            else:
                raise RuntimeError(f"Unknown generation mode: {mode}")

            card.status = "generated"
            card.approved = False
            write_asset_board(project_dir, cards)
            self.log_queue.put(f"__ASSET_STATUS__Кадр {card.shot_index:03d} generated")
            self.log_queue.put("__ASSET_BOARD_REFRESH__")
        except Exception as exc:
            if self.current_project_dir:
                try:
                    cards = load_asset_board(self.current_project_dir)
                    if 0 <= card_index < len(cards):
                        cards[card_index].status = "needs_review"
                        cards[card_index].notes = (cards[card_index].notes + "\n" if cards[card_index].notes else "") + f"Generation error: {exc}"
                        write_asset_board(self.current_project_dir, cards)
                except Exception:
                    pass
            self.log_queue.put(f"__ASSET_STATUS__Generation error: {exc}")
            self.log_queue.put("__ASSET_BOARD_REFRESH__")

    def start_render_selected_remotion(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Задача уже идет", "Дождитесь завершения текущего рендера.")
            return
        if self.current_asset_index is None or not self.current_project_dir:
            messagebox.showerror("Нет карточки", "Выберите Remotion-кадр на доске кадров.")
            return
        self.save_current_asset_card()
        cards = load_asset_board(self.current_project_dir)
        card = cards[self.current_asset_index]
        self.worker = threading.Thread(target=self.render_asset_remotion, args=(card, cards, self.current_asset_index), daemon=True)
        self.worker.start()

    def render_asset_remotion(self, card: AssetCard, cards: list[AssetCard], card_index: int):
        if not self.current_project_dir:
            return
        shots_path = self.current_project_dir / "04_shot_list.json"
        shots = [Shot(**item) for item in json.loads(shots_path.read_text(encoding="utf-8"))]
        shot = next((item for item in shots if item.index == card.shot_index), None)
        if not shot:
            raise RuntimeError(f"Кадр {card.shot_index:03d} не найден в списке кадров")
        renders_dir = self.current_project_dir / "renders" / "remotion"
        job = remotion_job_for_shot(shot, renders_dir)
        if not job:
            raise RuntimeError("Для выбранной карточки нет Remotion job.")
        jobs_dir = self.current_project_dir / "remotion_jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_path = jobs_dir / f"shot_{shot.index:03d}_{job['props']['template']}.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        cards[card_index].status = "generating"
        write_asset_board(self.current_project_dir, cards)
        self.log_queue.put("__ASSET_BOARD_REFRESH__")

    def render_asset_motion_text(self, card: AssetCard, cards: list[AssetCard], card_index: int):
        if not self.current_project_dir:
            return
        renders_dir = self.current_project_dir / "renders" / "remotion"
        job = remotion_motion_text_job_for_card(card, renders_dir)
        jobs_dir = self.current_project_dir / "remotion_jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_path = jobs_dir / f"shot_{card.shot_index:03d}_{job['props']['template']}.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        cards[card_index].status = "generating"
        write_asset_board(self.current_project_dir, cards)
        self.log_queue.put("__ASSET_BOARD_REFRESH__")
        self.render_remotion_jobs([job_path])
        output = Path(job.get("output", ""))
        fresh_cards = load_asset_board(self.current_project_dir)
        if 0 <= card_index < len(fresh_cards):
            if output.exists():
                fresh_cards[card_index].remotion_output = str(output)
                fresh_cards[card_index].status = "generated"
            else:
                fresh_cards[card_index].status = "needs_review"
            write_asset_board(self.current_project_dir, fresh_cards)
        self.log_queue.put("__ASSET_BOARD_REFRESH__")
        self.render_remotion_jobs([job_path])
        output = Path(job.get("output", ""))
        fresh_cards = load_asset_board(self.current_project_dir)
        if 0 <= card_index < len(fresh_cards):
            if output.exists():
                fresh_cards[card_index].remotion_output = str(output)
                fresh_cards[card_index].status = "generated"
            else:
                fresh_cards[card_index].status = "needs_review"
            write_asset_board(self.current_project_dir, fresh_cards)
        self.log_queue.put("__ASSET_BOARD_REFRESH__")

    def create_remotion_jobs(self):
        if not self.current_project_dir:
            guessed = self.output_base / slugify(self.title_var.get().strip() or "transcription")
            if guessed.exists():
                self.current_project_dir = guessed
                self.project_var.set(str(guessed))
        if not self.current_project_dir:
            messagebox.showerror("Нет проекта", "Сначала создайте проект через транскрибацию.")
            return
        shot_path = self.current_project_dir / "04_shot_list.json"
        if not shot_path.exists():
            messagebox.showerror("Нет списка кадров", "Сначала создайте список кадров в разделе 'Кадры'.")
            return
        shots = [Shot(**item) for item in json.loads(shot_path.read_text(encoding="utf-8"))]
        jobs = write_remotion_jobs(self.current_project_dir, shots)
        self.remotion_preview.delete("1.0", END)
        self.remotion_preview.insert(END, f"Создано Remotion job-файлов: {len(jobs)}\n\n")
        for job in jobs:
            self.remotion_preview.insert(END, str(job) + "\n")
        if not jobs:
            self.remotion_preview.insert(END, "В списке кадров нет элементов, подходящих для Remotion.\n")

    def start_remotion_render(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.current_project_dir:
            messagebox.showerror("Нет проекта", "Сначала выберите/создайте проект.")
            return
        jobs_dir = self.current_project_dir / "remotion_jobs"
        jobs = sorted(jobs_dir.glob("*.json")) if jobs_dir.exists() else []
        if not jobs:
            messagebox.showerror("Нет jobs", "Сначала нажмите 'Создать Remotion jobs'.")
            return
        self.remotion_preview.delete("1.0", END)
        self.worker = threading.Thread(target=self.render_remotion_jobs, args=(jobs,), daemon=True)
        self.worker.start()

    def clean_remotion_renders(self):
        if not self.current_project_dir:
            messagebox.showerror("Нет проекта", "Сначала выберите/создайте проект.")
            return
        renders_dir = self.current_project_dir / "renders" / "remotion"
        if not renders_dir.exists():
            self.remotion_preview.delete("1.0", END)
            self.remotion_preview.insert(END, "Папка renders/remotion еще не создана.\n")
            return
        deleted = 0
        for path in renders_dir.glob("*.mp4"):
            path.unlink()
            deleted += 1
        self.remotion_preview.delete("1.0", END)
        self.remotion_preview.insert(END, f"Удалено Remotion mp4: {deleted}\n")

    def render_remotion_jobs(self, jobs: list[Path]):
        try:
            remotion_dir = Path.cwd() / "remotion_graphics"
            node_modules = remotion_dir / "node_modules"
            if not node_modules.exists():
                self.log_queue.put(
                    "__REMOTION__Remotion dependencies are not installed.\n"
                    "Run this once:\n"
                    f"cd /d {remotion_dir}\n"
                    "npm install\n"
                )
                return

            node = shutil.which("node.exe") or shutil.which("node")
            if not node:
                self.log_queue.put("__REMOTION__ERROR: node.exe was not found in PATH.\n")
                return

            script = remotion_dir / "scripts" / "render-job.mjs"
            for index, job in enumerate(jobs, start=1):
                self.log_queue.put(f"__REMOTION__Rendering {index}/{len(jobs)}: {job.name}\n")
                try:
                    job_data = json.loads(job.read_text(encoding="utf-8"))
                    expected_output = Path(job_data.get("output", ""))
                except Exception:
                    expected_output = None
                command = [node, str(script), str(job)]
                self.log_queue.put(f"__REMOTION__Command: {' '.join(command)}\n")
                process = subprocess.Popen(
                    command,
                    cwd=remotion_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    self.log_queue.put("__REMOTION__" + line)
                code = process.wait()
                if code != 0:
                    self.log_queue.put(f"__REMOTION__Render failed for {job.name} with code {code}\n")
                    return
                if expected_output and expected_output.exists():
                    self.log_queue.put(
                        f"__REMOTION__Created: {expected_output} ({expected_output.stat().st_size} bytes)\n"
                    )
                elif expected_output:
                    self.log_queue.put(f"__REMOTION__WARNING: expected output was not found: {expected_output}\n")
            self.log_queue.put("__REMOTION__DONE\n")
        except Exception as exc:
            self.log_queue.put(f"__REMOTION__ERROR: {exc}\n")

    def generate_visual_plan(self, scenes: list[Scene], use_lm: bool):
        try:
            assert self.current_project_dir is not None
            if use_lm:
                self.log_queue.put("Visual stage 1/3: asking LM Studio for frame list")
                try:
                    tm_segments = getattr(self, "transcript_segments", [])
                    settings_snapshot = load_settings()
                    reference_density = (
                        settings_snapshot.reference_density
                        if settings_snapshot.reference_enable
                        else "off"
                    )
                    shots = call_lm_studio_for_shots(
                        self.lm_url.get(),
                        self.lm_model.get(),
                        scenes,
                        image_preference=self.image_model_preference.get(),
                        ttl_seconds=self.lm_ttl_seconds(),
                        segments=tm_segments,
                        video_style=DEFAULT_VIDEO_STYLE,
                        reference_density=reference_density,
                    )
                    self.log_queue.put(f"Visual stage 2/3: LM Studio returned {len(shots)} frame(s)")
                    self.schedule_lm_unload("frame list generation finished")
                except Exception as exc:
                    self.log_queue.put(f"LM Studio visual plan failed: {explain_lm_studio_error(exc)}")
                    self.log_queue.put("Falling back to local frame-list draft")
                    shots = build_heuristic_shots(scenes, image_preference=self.image_model_preference.get(), video_style=DEFAULT_VIDEO_STYLE)
            else:
                self.log_queue.put("Visual stage 1/3: building local frame-list draft")
                shots = build_heuristic_shots(scenes, image_preference=self.image_model_preference.get(), video_style=DEFAULT_VIDEO_STYLE)
            write_shot_list_files(self.current_project_dir, shots)
            preview = []
            for shot in shots:
                preview.append(
                    f"{shot.index:03d}. {shot.start}-{shot.end} | сцена {shot.scene_index} | "
                    f"{shot.visual_type} | {shot.description}"
                )
                if shot.image_model or shot.video_model:
                    preview.append(f"     models: {shot.image_model or '-'} / {shot.video_model or '-'}")
                if shot.screen_text:
                    preview.append(f"     text: {shot.screen_text}")
            self.log_queue.put("__VISUAL_PLAN__" + "\n".join(preview))
            self.log_queue.put("Visual stage 3/3: visual plan saved")
        except Exception as exc:
            self.log_queue.put(f"ERROR: {exc}")

    def analyze_scenes(self):
        try:
            assert self.current_project_dir is not None
            try:
                context_length = int(self.lm_context.get().strip())
            except ValueError:
                context_length = 32768
            self.log_queue.put(f"Scene stage 0/3: ensuring LM Studio model is loaded with context_length={context_length}")
            load_result = load_lm_studio_model(self.lm_url.get(), self.lm_model.get(), context_length)
            load_config = load_result.get("load_config", {})
            self.log_queue.put(
                "LM Studio active context_length="
                f"{load_config.get('context_length', 'unknown')}"
            )
            char_count = sum(len(segment.text) for segment in self.transcript_segments)
            self.log_queue.put(
                "Scene stage 1/3: sending FULL timestamped transcript to LM Studio "
                f"({len(self.transcript_segments)} segments, ~{char_count} chars)"
            )
            try:
                scenes = call_lm_studio_for_segments(
                    self.lm_url.get(),
                    self.lm_model.get(),
                    self.transcript_segments,
                    ttl_seconds=self.lm_ttl_seconds(),
                )
                self.log_queue.put(f"Scene stage 2/3: LM Studio returned {len(scenes)} scene(s)")
            except Exception as exc:
                self.log_queue.put(f"LM Studio scene analysis failed: {explain_lm_studio_error(exc)}")
                self.log_queue.put("Scene stage 2/3: building local scenes from transcript timestamps")
                scenes = build_scenes_from_segments(self.transcript_segments)
            write_scene_analysis_files(self.current_project_dir, scenes)
            preview = []
            for scene in scenes:
                preview.append(f"{scene.index:02d}. {scene.start}-{scene.end} | {scene.title}")
                if scene.screen_text:
                    preview.append(f"    Экран: {scene.screen_text}")
                if scene.retention_note:
                    preview.append(f"    Удержание: {scene.retention_note}")
            self.log_queue.put("__SCENES__" + "\n".join(preview))
            self.log_queue.put("Scene stage 3/3: analysis saved")
            self.log_queue.put("__SCENE_STATUS__Сцены готовы")
            self.schedule_lm_unload("scene analysis finished")
        except Exception as exc:
            self.log_queue.put(f"ERROR: {explain_lm_studio_error(exc)}")
            self.log_queue.put("__SCENE_STATUS__Ошибка анализа")

    def generate_pack(self, title: str, script_text: str):
        try:
            project_dir = self.current_project_dir or self.output_base / slugify(title)
            self.log_queue.put(f"Project: {project_dir}")
            self.log_queue.put("Stage 1/4: preparing text")

            scenes: list[Scene]
            if self.lm_enabled.get():
                self.log_queue.put("Stage 2/4: asking LM Studio for scene plan")
                try:
                    scenes = call_lm_studio(
                        self.lm_url.get(),
                        self.lm_model.get(),
                        script_text,
                        ttl_seconds=self.lm_ttl_seconds(),
                    )
                    self.log_queue.put(f"LM Studio returned {len(scenes)} scene(s)")
                    self.schedule_lm_unload("production pack generation finished")
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError) as exc:
                    self.log_queue.put(f"LM Studio failed: {exc}")
                    self.log_queue.put("Falling back to local heuristic scene split")
                    scenes = build_heuristic_scenes(script_text)
            else:
                if self.transcript_segments:
                    self.log_queue.put("Stage 2/4: local scene split from real transcript timestamps")
                    scenes = build_scenes_from_segments(self.transcript_segments)
                else:
                    self.log_queue.put("Stage 2/4: local scene split")
                    scenes = build_heuristic_scenes(script_text)

            self.log_queue.put(f"Stage 3/4: generated {len(scenes)} scene(s)")
            self.log_queue.put("Stage 4/4: writing files")
            write_project_files(
                project_dir,
                title,
                self.audio_path,
                script_text,
                scenes,
                transcript_segments=self.transcript_segments,
                whisper_json_path=self.whisper_json_path,
                whisper_srt_path=self.whisper_srt_path,
            )
            self.log_queue.put("DONE")
            self.log_queue.put(f"Output folder: {project_dir}")
            self.log_queue.put("__STATUS__Готово")
        except Exception as exc:
            self.log_queue.put(f"ERROR: {exc}")
            self.log_queue.put("__STATUS__Ошибка")

    def poll_log_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                if message.startswith("__STATUS__"):
                    self.status_var.set(message.replace("__STATUS__", "", 1))
                elif message.startswith("__SCENE_STATUS__"):
                    self.scene_status_var.set(message.replace("__SCENE_STATUS__", "", 1))
                elif message.startswith("__PROJECT__"):
                    self.project_var.set(message.replace("__PROJECT__", "", 1))
                elif message.startswith("__SCRIPT__"):
                    script = message.replace("__SCRIPT__", "", 1)
                    self.transcription_text.delete("1.0", END)
                    self.transcription_text.insert(END, script)
                    self.script_text.delete("1.0", END)
                    self.script_text.insert(END, script)
                elif message.startswith("__SCENES__"):
                    self.scene_preview.delete("1.0", END)
                    self.scene_preview.insert(END, message.replace("__SCENES__", "", 1))
                elif message.startswith("__VISUAL_PLAN__"):
                    self.visual_plan_preview.delete("1.0", END)
                    self.visual_plan_preview.insert(END, message.replace("__VISUAL_PLAN__", "", 1))
                elif message.startswith("__REMOTION__"):
                    self.remotion_preview.insert(END, message.replace("__REMOTION__", "", 1))
                    self.remotion_preview.see(END)
                elif message.startswith("__ASSET_STATUS__"):
                    self.asset_board_status_var.set(message.replace("__ASSET_STATUS__", "", 1))
                elif message.startswith("__ASSET_BOARD_REFRESH__"):
                    current_index = self.current_asset_index
                    self.load_asset_board_into_ui()
                    self.select_asset_card(current_index)
                elif message.startswith("__LM_UNLOAD_STATUS__"):
                    status = message.replace("__LM_UNLOAD_STATUS__", "", 1)
                    if hasattr(self, "lm_unload_status_var"):
                        self.lm_unload_status_var.set(status)
                    self.scene_status_var.set(status)
                else:
                    self.log.insert(END, message + "\n")
                    self.log.see(END)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_log_queue)


def main():
    os.chdir(Path(__file__).resolve().parent)
    root = Tk()
    ContentPipelineApp(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
