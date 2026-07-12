"""Scene segmentation and LM-assisted scene labelling.

This module is the UI-independent home for the scene-analysis stage.  It keeps
the established pipeline behaviour while depending only on reusable core
models, style presets, the LM payload helper, and the Python standard library.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import urllib.request

from core import lm as lm_client
from core.models import (
    Scene,
    TranscriptSegment,
    clean_script_text,
    estimate_duration_seconds,
    make_screen_text,
    seconds_to_timecode,
    split_into_blocks,
    timecode_to_seconds,
)
from core.style_presets import (
    DEFAULT_VIDEO_STYLE,
    get_video_style,
    style_director_brief,
)


def make_title(text: str, index: int) -> str:
    """Return the established short scene title fallback."""
    sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " ").strip())
    first = sentences[0] if sentences else f"Сцена {index}"
    first = re.sub(r"\s+", " ", first).strip()
    if len(first) > 58:
        first = first[:55].rsplit(" ", 1)[0] + "..."
    return first or f"Сцена {index}"


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


def build_heuristic_scenes(script_text: str) -> list[Scene]:
    scenes: list[Scene] = []
    cursor = 0
    for index, block in enumerate(split_into_blocks(script_text), start=1):
        duration = estimate_duration_seconds(block)
        scenes.append(
            Scene(
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
        )
        cursor += duration
    return scenes


def build_scenes_from_segments(
    segments: list[TranscriptSegment], target_seconds: int = 45
) -> list[Scene]:
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


def _lm_embeddings(
    base_url: str,
    model: str,
    texts: list[str],
    timeout: int = 120,
    ttl_seconds: int | None = None,
) -> list[list[float]]:
    """Fetch vectors from an OpenAI-compatible ``/embeddings`` endpoint."""
    endpoint = base_url.rstrip("/") + "/embeddings"
    payload: dict = {"model": model, "input": texts}
    lm_client.add_ttl(payload, ttl_seconds)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    items = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
    embeddings = [item["embedding"] for item in items]
    if len(embeddings) != len(texts):
        raise ValueError(f"embedding count mismatch: {len(embeddings)} != {len(texts)}")
    return embeddings


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _mean_vec(vecs: list[list[float]]) -> list[float]:
    n = len(vecs)
    dim = len(vecs[0])
    return [sum(vector[d] for vector in vecs) / n for d in range(dim)]


def _merge_segments_into_units(
    segments: list[TranscriptSegment],
    min_words: int = 12,
    max_unit_seconds: float = 14.0,
) -> list[list[TranscriptSegment]]:
    """Group tiny Whisper fragments into sentence-sized embedding units."""
    units: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    words = 0
    unit_start = 0.0
    for segment in segments:
        if not current:
            unit_start = timecode_to_seconds(segment.start)
        current.append(segment)
        words += len((segment.text or "").split())
        duration = timecode_to_seconds(segment.end) - unit_start
        ends_sentence = (segment.text or "").rstrip().endswith((".", "!", "?", "…"))
        if (words >= min_words and ends_sentence) or duration >= max_unit_seconds:
            units.append(current)
            current = []
            words = 0
    if current:
        units.append(current)
    return units


def _block_similarity(embeddings: list[list[float]], gap: int, window: int) -> float:
    left = embeddings[max(0, gap - window + 1) : gap + 1]
    right = embeddings[gap + 1 : gap + 1 + window]
    if not left or not right:
        return 1.0
    return _cosine_sim(_mean_vec(left), _mean_vec(right))


def _depth_scores(gap_scores: list[float]) -> list[float]:
    n = len(gap_scores)
    depths = [0.0] * n
    for index in range(n):
        left = index
        while left > 0 and gap_scores[left - 1] >= gap_scores[left]:
            left -= 1
        right = index
        while right < n - 1 and gap_scores[right + 1] >= gap_scores[right]:
            right += 1
        depths[index] = (
            gap_scores[left] - gap_scores[index]
            + gap_scores[right] - gap_scores[index]
        )
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
    """Build deterministic TextTiling-style scene boundaries from embeddings."""
    if not segments:
        return []

    units = _merge_segments_into_units(segments, min_words=min_unit_words)
    unit_texts = [
        " ".join((segment.text or "").strip() for segment in unit).strip() or " "
        for unit in units
    ]
    try:
        embeddings = _lm_embeddings(
            base_url, model, unit_texts, ttl_seconds=ttl_seconds
        )
    except Exception:
        if fallback:
            return build_scenes_from_segments(segments)
        raise

    if len(units) <= 1:
        flat = [segment for unit in units for segment in unit] or segments
        return [make_scene_from_segments(1, flat)]

    gap_count = len(units) - 1
    block_window = max(1, int(window))
    gaps = [
        _block_similarity(embeddings, index, block_window)
        for index in range(gap_count)
    ]
    depths = _depth_scores(gaps)

    if target_scenes and target_scenes > 1:
        order = sorted(range(gap_count), key=lambda index: depths[index], reverse=True)
        chosen = set(order[: max(0, int(target_scenes) - 1)])
    else:
        mean_depth = statistics.fmean(depths) if depths else 0.0
        std_depth = statistics.pstdev(depths) if len(depths) > 1 else 0.0
        cutoff = mean_depth + (0.5 - sensitivity) * std_depth
        chosen = {
            index
            for index in range(gap_count)
            if depths[index] > 0 and depths[index] >= cutoff
        }

    groups: list[list[int]] = []
    start = 0
    for index in range(gap_count):
        if index in chosen:
            groups.append(list(range(start, index + 1)))
            start = index + 1
    groups.append(list(range(start, len(units))))

    def group_duration(group: list[int]) -> float:
        return timecode_to_seconds(units[group[-1]][-1].end) - timecode_to_seconds(
            units[group[0]][0].start
        )

    changed = True
    while changed and len(groups) > 1:
        changed = False
        for index, group in enumerate(groups):
            if group_duration(group) < min_seconds:
                if index + 1 < len(groups):
                    groups[index + 1] = group + groups[index + 1]
                else:
                    groups[index - 1] = groups[index - 1] + group
                groups.pop(index)
                changed = True
                break

    def split_long(group: list[int]) -> list[list[int]]:
        if group_duration(group) <= max_seconds or len(group) < 2:
            return [group]
        cut = max(group[:-1], key=lambda unit_index: depths[unit_index])
        left = [unit_index for unit_index in group if unit_index <= cut]
        right = [unit_index for unit_index in group if unit_index > cut]
        return split_long(left) + split_long(right)

    final: list[list[int]] = []
    for group in groups:
        final.extend(split_long(group))

    if log_callback:
        log_callback(
            f"TextTiling: реплик={len(units)}, границ={len(final) - 1}, сцен={len(final)} "
            f"(sensitivity={sensitivity}, target={target_scenes or 'auto'}, окно={block_window})"
        )

    scenes: list[Scene] = []
    for scene_index, group in enumerate(final, start=1):
        scene_segments = [
            segment for unit_index in group for segment in units[unit_index]
        ]
        scenes.append(make_scene_from_segments(scene_index, scene_segments))
    return scenes


def clean_lm_json_content(content: str) -> str:
    content = (content or "").strip()
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    content = re.sub(r':\s*\[([^\]]+)\]"\s*([,\}\]])', r": [\1]\2", content)
    return content.strip()


def parse_lm_json_array(content: str, label: str = "LM Studio") -> list:
    """Parse the first LM-produced JSON array, tolerating known reply damage."""
    content = clean_lm_json_content(content)
    start = content.find("[")
    if start < 0:
        raise ValueError(
            f"{label} не вернул JSON-массив. Попробуйте повторить запрос "
            "или уменьшить длину транскрипции."
        )
    candidate = content[start:]

    decoder = json.JSONDecoder()
    decoder_lenient = json.JSONDecoder(strict=False)

    def try_decode(text: str):
        for decoder_instance in (decoder, decoder_lenient):
            try:
                return decoder_instance.raw_decode(text)
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

    result = try_decode(candidate)
    if result is not None:
        parsed, _ = result
        if isinstance(parsed, list):
            return parsed

    end = candidate.rfind("]")
    if end > 0:
        result = try_decode(candidate[: end + 1])
        if result is not None:
            parsed, _ = result
            if isinstance(parsed, list):
                return parsed

    try:
        fixed = candidate.rstrip()
        if fixed[-1] != "]":
            last_object_end = fixed.rfind("}")
            if last_object_end > 0:
                fixed = fixed[: last_object_end + 1] + "]"
        result = try_decode(fixed)
        if result is not None:
            parsed, _ = result
            if isinstance(parsed, list):
                return parsed
    except (json.JSONDecodeError, IndexError):
        pass

    raise ValueError(
        f"{label} вернул поврежденный JSON (len={len(candidate)}). "
        f"HEAD: {candidate[:300]!r} ... TAIL: {candidate[-300:]!r}. "
        "Повторите анализ; если повторяется, уменьшите длину транскрипции "
        "или выберите более стабильную модель."
    )


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
    """Fill creative fields without changing fixed scene boundaries."""
    if not scenes:
        return scenes
    style = get_video_style(video_style)
    style_block = "\n" + style_director_brief(video_style, style_anchor) + "\n"
    character_block = (
        f"ПЕРСОНАЖ (одинаков во всех сценах): {character_description}\n"
        if character_description
        else ""
    )
    compact = "\n".join(
        f"{scene.index}|{scene.start}-{scene.end}|{(scene.voice_text or '').strip()}"
        for scene in scenes
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
    payload: dict = {
        "model": model.strip() or "local-model",
        "messages": [
            {
                "role": "system",
                "content": "Отвечай только валидным JSON. Не добавляй markdown. visual_direction — буквальная физическая сцена, без метафор и символов.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }
    lm_client.add_ttl(payload, ttl_seconds)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
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
        scene.visual_direction = (
            str(item.get("visual_direction", scene.visual_direction)).strip()
            or scene.visual_direction
        )
        scene.screen_text = str(item.get("screen_text", scene.screen_text)).strip()
        scene.music_note = (
            str(item.get("music_note", scene.music_note)).strip() or scene.music_note
        )
        scene.retention_note = (
            str(item.get("retention_note", scene.retention_note)).strip()
            or scene.retention_note
        )
    return scenes


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
    """Turn an un-timed narration script into LM-labelled scene blocks."""
    del niche  # retained for compatibility with analyzer/plugin call sites
    endpoint = base_url.rstrip("/") + "/chat/completions"
    selected_style = get_video_style(video_style)
    style_block = "\n" + style_director_brief(video_style, style_anchor) + "\n"
    character_block = (
        f"ПЕРСОНАЖ (должен быть одинаков во всех сценах): {character_description}\n"
        if character_description
        else ""
    )
    prompt = (
        f'Ты режиссёр монтажа YouTube-контента в стиле "{selected_style.name}".\n\n'
        "Разбей русский дикторский текст на сцены для монтажа.\n"
        "Для КАЖДОЙ сцены РАЗВЕРНИ текст в визуальную сцену:\n"
        "- Какую локацию, время суток, освещение мы видим?\n"
        "- Кто или что в кадре? (персонаж, объект, техника, природа)\n"
        "- Какое действие происходит?\n"
        "- С какого ракурса снят кадр? (общий план, средний, деталь, сверху)\n"
        f"{style_block}{character_block}"
        "Правила:\n"
        "- visual_direction должен быть КОНКРЕТНЫМ: не просто 'Офис', а "
        "'Open-plan office, late afternoon, warm sunlight through venetian blinds, medium shot'\n"
        "- Не повторяй один и тот же тип кадра/локацию в соседних сценах\n"
        "- Чередуй: если сцена 1 — интерьер/крупный план, то сцена 2 — экстерьер/общий план\n"
        "Верни только JSON-массив без markdown. Каждый объект: "
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
    lm_client.add_ttl(payload, ttl_seconds)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        data = json.loads(response.read().decode("utf-8"))

    raw_scenes = parse_lm_json_array(
        data["choices"][0]["message"]["content"].strip(),
        "LM Studio scene analysis",
    )
    scenes: list[Scene] = []
    cursor = 0
    for index, item in enumerate(raw_scenes, start=1):
        if not isinstance(item, dict):
            continue
        voice_text = str(item.get("voice_text", "")).strip()
        duration = estimate_duration_seconds(voice_text)
        scenes.append(
            Scene(
                index=len(scenes) + 1,
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
        if character_description
        else ""
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
    payload: dict = {
        "model": model.strip() or "local-model",
        "messages": [
            {
                "role": "system",
                "content": "Отвечай только валидным JSON. Не добавляй markdown.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
    }
    lm_client.add_ttl(payload, ttl_seconds)
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
        if not isinstance(raw, dict):
            continue
        ids: list[int] = []
        for item in raw.get("segment_ids") or []:
            try:
                segment_id = int(item)
            except (TypeError, ValueError):
                continue
            if segment_id in by_id and segment_id > last_id:
                ids.append(segment_id)
        ids = sorted(set(ids))
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
                retention_note=str(
                    raw.get("retention_note", raw.get("purpose", ""))
                ).strip(),
            )
        )
        last_id = ids[-1]

    if last_id < len(segments):
        scenes.extend(build_scenes_from_segments(segments[last_id:]))
        for index, scene in enumerate(scenes, start=1):
            scene.index = index

    return _merge_similar_scenes(scenes)


def _merge_similar_scenes(scenes: list[Scene]) -> list[Scene]:
    if len(scenes) < 2:
        return scenes
    merged: list[Scene] = []
    for scene in scenes:
        if not merged:
            merged.append(scene)
            continue
        previous = merged[-1]
        previous_words = {
            word.lower()
            for word in (previous.title + " " + previous.visual_direction).split()
            if len(word) > 3
        }
        current_words = {
            word.lower()
            for word in (scene.title + " " + scene.visual_direction).split()
            if len(word) > 3
        }
        if not previous_words or not current_words:
            merged.append(scene)
            continue
        overlap = len(previous_words & current_words)
        similarity = overlap / max(len(previous_words), len(current_words))
        if similarity > 0.45:
            previous.end = scene.end
            previous.voice_text = (
                previous.voice_text + " " + scene.voice_text
            ).strip()
            previous.retention_note = (
                previous.retention_note + "; " + scene.retention_note
            ).strip()
        else:
            merged.append(scene)
    for index, scene in enumerate(merged, start=1):
        scene.index = index
    return merged
