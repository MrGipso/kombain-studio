"""Scene segmentation and LM-assisted scene labelling.

This module is the UI-independent home for the scene-analysis stage.  It keeps
the established pipeline behaviour while depending only on reusable core
models, style presets, the LM payload helper, and the Python standard library.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
import urllib.request
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field

from core import lm as lm_client
from core.models import (
    Scene,
    TranscriptSegment,
    clean_script_text,
    estimate_duration_seconds,
    make_screen_text,
    normalize_numeric_screen_text,
    seconds_to_timecode,
)
from core.style_presets import (
    DEFAULT_VIDEO_STYLE,
    get_video_style,
    style_director_brief,
)


logger = logging.getLogger(__name__)


_SCENE_ENRICHMENT_MAX_SCENES = 8
_SCENE_ENRICHMENT_MAX_VOICE_CHARS = 12_000


class SceneEnrichmentLabel(BaseModel):
    """Creative metadata returned by the LM for one immutable scene."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    # Bounds are deliberately looser than the values actually stored: an
    # over-long field is a formatting slip, not a reason to discard the whole
    # batch and fall back to a different segmentation strategy.  The strict
    # editorial limits are applied in _apply_scene_enrichment_batch, which
    # clamps or blanks the field exactly like the plain-JSON path does.
    index: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=400)
    visual_direction: str = Field(min_length=1, max_length=4_000)
    screen_text: str = Field(max_length=400)
    music_note: str = Field(max_length=1_000)
    retention_note: str = Field(max_length=1_000)


class SceneEnrichmentEnvelope(BaseModel):
    """Strict response envelope for one bounded enrichment batch."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenes: list[SceneEnrichmentLabel] = Field(
        min_length=1,
        max_length=_SCENE_ENRICHMENT_MAX_SCENES,
    )


def _precise_timecode_seconds(value: str) -> float:
    """Parse pipeline/legacy timecodes without discarding sub-second precision."""
    text = str(value or "").strip().replace(",", ".")
    parts = text.split(":")
    try:
        if len(parts) == 3:
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            seconds = int(parts[0]) * 60 + float(parts[1])
        else:
            seconds = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid timecode: {value!r}") from exc
    if not math.isfinite(seconds):
        raise ValueError(f"non-finite timecode: {value!r}")
    return seconds


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


def close_scene_timeline_gaps(
    scenes: list[Scene],
    *,
    max_gap_seconds: int = 2,
) -> list[Scene]:
    """Absorb short transcript-rounding pauses into the preceding scene.

    Whisper/imported subtitle timestamps commonly leave one-second holes even
    though the source video is continuous.  Keeping the narration text intact,
    extend the preceding edit scene to the next scene start so downstream shot
    scheduling never creates black timeline gaps.
    """
    limit = max(0, int(max_gap_seconds))
    for current, following in zip(scenes, scenes[1:]):
        current_end = _precise_timecode_seconds(current.end)
        following_start = _precise_timecode_seconds(following.start)
        gap = following_start - current_end
        if 0 < gap <= limit:
            current.end = following.start
    return scenes



def build_scenes_from_segments(
    segments: list[TranscriptSegment], target_seconds: int = 45
) -> list[Scene]:
    scenes: list[Scene] = []
    current: list[TranscriptSegment] = []
    scene_start_seconds = 0

    for segment in segments:
        if not current:
            scene_start_seconds = _precise_timecode_seconds(segment.start)
        current.append(segment)
        duration = _precise_timecode_seconds(segment.end) - scene_start_seconds
        words = sum(len(item.text.split()) for item in current)
        if duration >= target_seconds or words >= 100:
            scenes.append(make_scene_from_segments(len(scenes) + 1, current))
            current = []

    if current:
        scenes.append(make_scene_from_segments(len(scenes) + 1, current))

    return close_scene_timeline_gaps(scenes)


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
        headers=lm_client.request_headers(endpoint),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    raw_items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        raise ValueError("embedding response must contain a data array")
    try:
        items = sorted(raw_items, key=lambda item: item["index"])
    except (KeyError, TypeError) as exc:
        raise ValueError("embedding response contains an invalid item") from exc
    indices = [item.get("index") for item in items if isinstance(item, dict)]
    if any(type(index) is not int for index in indices) or indices != list(range(len(texts))):
        raise ValueError(
            f"embedding indexes mismatch: {indices!r} != {list(range(len(texts)))!r}"
        )

    embeddings: list[list[float]] = []
    dimension: int | None = None
    for item in items:
        vector = item.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise ValueError("embedding vector must be a non-empty array")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in vector
        ):
            raise ValueError("embedding vector contains a non-finite or non-numeric value")
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise ValueError(
                f"embedding dimension mismatch: {len(vector)} != {dimension}"
            )
        embeddings.append([float(value) for value in vector])
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
            unit_start = _precise_timecode_seconds(segment.start)
        current.append(segment)
        words += len((segment.text or "").split())
        duration = _precise_timecode_seconds(segment.end) - unit_start
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


def _is_exact_index_partition(groups: list[list[int]], item_count: int) -> bool:
    """Return whether *groups* cover ``range(item_count)`` exactly once.

    Scene construction has several repair/fallback paths.  Keeping this small
    invariant check next to the partitioners prevents a locally plausible
    group list from silently dropping, duplicating, or reordering narration.
    """
    cursor = 0
    for group in groups:
        if not group or group != list(range(cursor, cursor + len(group))):
            return False
        cursor += len(group)
    return cursor == item_count


def _validate_scene_partition(
    scenes: list[Scene], segments: list[TranscriptSegment]
) -> bool:
    """Validate exact transcript coverage and monotone scene metadata.

    The validator deliberately compares ``voice_text`` with the source
    transcript instead of trusting LLM-produced text.  It accepts gaps between
    source timecodes, but rejects backwards scene starts/ends and requires each
    scene boundary to coincide with a source segment boundary.
    """
    if not segments:
        return not scenes
    if not scenes:
        return False

    cursor = 0
    previous_end: float | None = None
    for expected_index, scene in enumerate(scenes, start=1):
        if scene.index != expected_index or cursor >= len(segments):
            return False
        if scene.start != segments[cursor].start:
            return False

        try:
            start_seconds = _precise_timecode_seconds(scene.start)
            end_seconds = _precise_timecode_seconds(scene.end)
        except (TypeError, ValueError):
            return False
        if end_seconds < start_seconds:
            return False
        if previous_end is not None and start_seconds < previous_end:
            return False

        text_parts: list[str] = []
        matched_end: int | None = None
        for segment_index in range(cursor, len(segments)):
            segment = segments[segment_index]
            text_parts.append((segment.text or "").strip())
            voice_matches = (
                " ".join(text_parts).strip() == (scene.voice_text or "").strip()
            )
            closes_on_segment = segment.end == scene.end
            closes_on_next_start = (
                segment_index + 1 < len(segments)
                and segments[segment_index + 1].start == scene.end
            )
            if voice_matches and (closes_on_segment or closes_on_next_start):
                matched_end = segment_index + 1
                break
        if matched_end is None:
            return False

        cursor = matched_end
        previous_end = end_seconds

    return cursor == len(segments)


def _constrained_unit_partition(
    units: list[list[TranscriptSegment]],
    depths: list[float],
    preferred_boundaries: set[int],
    *,
    min_seconds: int,
    max_seconds: int,
    desired_scenes: int,
) -> list[list[int]]:
    """Partition units while satisfying duration limits whenever possible.

    This is a compact dynamic program over contiguous unit ranges.  Duration
    violations are minimized first; among equally valid partitions it chooses
    the scene count closest to ``desired_scenes`` and then the strongest
    semantic boundaries.  Consequently a requested target never creates short
    fragments when a zero-violation partition exists, while indivisible long
    units and awkward tails still receive a deterministic best-effort result.
    """
    item_count = len(units)
    if item_count == 0:
        return []

    minimum = max(0.0, float(min_seconds))
    maximum = max(minimum, float(max_seconds))
    desired = max(1, min(int(desired_scenes), item_count))
    preferred_bonus = max(depths, default=0.0) + 1.0

    def duration(start: int, end: int) -> float:
        return _precise_timecode_seconds(
            units[end - 1][-1].end
        ) - _precise_timecode_seconds(
            units[start][0].start
        )

    # states[position][count] =
    # (total duration violation, semantic score, previous position, previous count)
    states: list[dict[int, tuple[float, float, int | None, int | None]]] = [
        {} for _ in range(item_count + 1)
    ]
    states[0][0] = (0.0, 0.0, None, None)

    for start in range(item_count):
        if not states[start]:
            continue
        for end in range(start + 1, item_count + 1):
            span = duration(start, end)
            shortfall = max(0.0, minimum - span)
            overflow = max(0.0, span - maximum)
            violation = shortfall + overflow
            boundary_score = 0.0
            if end < item_count:
                gap_index = end - 1
                boundary_score = depths[gap_index]
                if gap_index in preferred_boundaries:
                    boundary_score += preferred_bonus

            for count, state in list(states[start].items()):
                new_count = count + 1
                candidate = (
                    state[0] + violation,
                    state[1] + boundary_score,
                    start,
                    count,
                )
                current = states[end].get(new_count)
                if current is None or (candidate[0], -candidate[1]) < (
                    current[0],
                    -current[1],
                ):
                    states[end][new_count] = candidate

            # With monotone transcript timecodes, every later range can only
            # increase an existing overflow.  Keeping the first over-limit
            # range permits best-effort recovery without making the DP cubic.
            if span > maximum:
                break

    if not states[item_count]:
        return [list(range(item_count))]

    final_count, _ = min(
        states[item_count].items(),
        key=lambda item: (
            item[1][0],
            abs(item[0] - desired),
            -item[1][1],
            item[0],
        ),
    )
    groups: list[list[int]] = []
    end = item_count
    count = final_count
    while end > 0:
        state = states[end][count]
        start = state[2]
        previous_count = state[3]
        if start is None or previous_count is None:
            return [list(range(item_count))]
        groups.append(list(range(start, end)))
        end = start
        count = previous_count
    groups.reverse()
    return groups


def partition_segments_by_embedding(
    segments: list[TranscriptSegment],
    base_url: str,
    model: str = "text-embedding-nomic-embed-text-v1.5",
    *,
    min_seconds: int = 20,
    max_seconds: int = 90,
    target_seconds: int = 45,
    sensitivity: float = 0.6,
    window: int = 2,
    min_unit_words: int = 12,
    ttl_seconds: int | None = None,
    log_callback=None,
) -> list[list[TranscriptSegment]]:
    """Semantic edit-scene partition of one transcript range.

    Same contract as :func:`partition_segments_by_duration` — every source
    segment is returned exactly once and the duration limits still win — but
    the cut points land where the topic actually shifts instead of on a
    stopwatch.  Used inside authored script chapters so the author's structure
    stays intact while the cuts within a chapter remain semantic.

    Falls back to the duration partitioner on any failure: no embedding
    endpoint, a degenerate range, or a partition that does not cover the input
    exactly once.  Scene boundaries are too important to leave to a partial
    result.
    """

    def _fallback(reason: str) -> list[list[TranscriptSegment]]:
        if log_callback and reason:
            log_callback(
                f"Резки внутри главы: эмбеддинги не применились ({reason}); "
                "использую разбиение по длительности."
            )
        return partition_segments_by_duration(
            segments,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            target_seconds=target_seconds,
        )

    if not segments:
        return []

    units = _merge_segments_into_units(segments, min_words=min_unit_words)
    if len(units) <= 1:
        # Nothing to cut inside: one unit is one scene by construction.
        return [list(segments)]

    unit_texts = [
        " ".join((segment.text or "").strip() for segment in unit).strip() or " "
        for unit in units
    ]
    try:
        embeddings = _lm_embeddings(
            base_url, model, unit_texts, ttl_seconds=ttl_seconds
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure degrades
        return _fallback(str(exc)[:160])

    gap_count = len(units) - 1
    block_window = max(1, int(window))
    gaps = [
        _block_similarity(embeddings, index, block_window)
        for index in range(gap_count)
    ]
    depths = _depth_scores(gaps)

    span = _precise_timecode_seconds(segments[-1].end) - _precise_timecode_seconds(
        segments[0].start
    )
    desired = max(1, min(len(units), round(span / max(1.0, float(target_seconds)))))

    if desired > 1:
        order = sorted(range(gap_count), key=lambda index: depths[index], reverse=True)
        chosen = set(order[: desired - 1])
    else:
        mean_depth = statistics.fmean(depths) if depths else 0.0
        std_depth = statistics.pstdev(depths) if len(depths) > 1 else 0.0
        cutoff = mean_depth + (0.5 - sensitivity) * std_depth
        chosen = {
            index
            for index in range(gap_count)
            if depths[index] > 0 and depths[index] >= cutoff
        }

    groups = _constrained_unit_partition(
        units,
        depths,
        chosen,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        desired_scenes=desired,
    )
    if not _is_exact_index_partition(groups, len(units)):
        return _fallback("разбиение не покрыло главу целиком")

    partition = [
        [segment for unit_index in group for segment in units[unit_index]]
        for group in groups
    ]
    if sum(len(group) for group in partition) != len(segments):
        return _fallback("потеряны сегменты при сборке")
    return partition


def partition_segments_by_duration(
    segments: list[TranscriptSegment],
    *,
    min_seconds: int = 20,
    max_seconds: int = 90,
    target_seconds: int = 45,
) -> list[list[TranscriptSegment]]:
    """Split a transcript range into balanced, sentence-aligned edit scenes.

    This is intentionally independent from embeddings: authored script beats
    already provide the semantic chapter boundaries.  Inside each chapter we
    only need deterministic edit-sized scenes whose boundaries remain attached
    to real transcript timestamps.

    Duration constraints have priority.  Among equally valid partitions the
    dynamic program prefers scene lengths near ``target_seconds`` and natural
    sentence/pause boundaries.  Every source segment is returned exactly once.
    """
    if not segments:
        return []

    minimum = max(0.0, float(min_seconds))
    maximum = max(minimum, float(max_seconds))
    target = min(maximum, max(minimum or 1.0, float(target_seconds)))
    item_count = len(segments)
    total_duration = max(
        0.0,
        _precise_timecode_seconds(segments[-1].end)
        - _precise_timecode_seconds(segments[0].start),
    )
    desired = max(1, int(round(total_duration / target)))
    desired = max(desired, int(math.ceil(total_duration / maximum)))
    desired = min(desired, item_count)

    # states[position][count] =
    # (duration_violation, target_deviation, -boundary_quality, prev_position)
    states: list[
        dict[int, tuple[float, float, float, int | None]]
    ] = [{} for _ in range(item_count + 1)]
    states[0][0] = (0.0, 0.0, 0.0, None)

    def span_duration(start: int, end: int) -> float:
        return (
            _precise_timecode_seconds(segments[end - 1].end)
            - _precise_timecode_seconds(segments[start].start)
        )

    def boundary_quality(end: int) -> float:
        if end >= item_count:
            return 0.0
        previous = segments[end - 1]
        following = segments[end]
        score = 1.0 if (previous.text or "").rstrip().endswith((".", "!", "?", "…")) else 0.0
        gap = (
            _precise_timecode_seconds(following.start)
            - _precise_timecode_seconds(previous.end)
        )
        return score + min(2.0, max(0.0, gap))

    for start in range(item_count):
        if not states[start]:
            continue
        for end in range(start + 1, item_count + 1):
            duration = span_duration(start, end)
            violation = max(0.0, minimum - duration) + max(0.0, duration - maximum)
            deviation = abs(duration - target)
            quality = boundary_quality(end)
            for count, state in list(states[start].items()):
                new_count = count + 1
                candidate = (
                    state[0] + violation,
                    state[1] + deviation,
                    state[2] - quality,
                    start,
                )
                current = states[end].get(new_count)
                if current is None or candidate[:3] < current[:3]:
                    states[end][new_count] = candidate
            # Keep the first over-limit candidate for best-effort recovery, but
            # later endpoints can only increase the overflow.
            if duration > maximum:
                break

    if not states[item_count]:
        return [list(segments)]

    final_count, _ = min(
        states[item_count].items(),
        key=lambda item: (
            item[1][0],
            abs(item[0] - desired),
            item[1][1],
            item[1][2],
            item[0],
        ),
    )
    ranges: list[tuple[int, int]] = []
    end = item_count
    count = final_count
    while end > 0:
        state = states[end][count]
        start = state[3]
        if start is None:
            return [list(segments)]
        ranges.append((start, end))
        end = start
        count -= 1
    ranges.reverse()

    groups = [segments[start:end] for start, end in ranges]
    flattened = [segment for group in groups for segment in group]
    if flattened != segments:
        return [list(segments)]
    return groups


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

    try:
        requested_target = max(0, int(target_scenes))
    except (TypeError, ValueError):
        requested_target = 0

    if requested_target:
        requested_target = min(requested_target, len(units))
        order = sorted(range(gap_count), key=lambda index: depths[index], reverse=True)
        chosen = set(order[: requested_target - 1])
    else:
        mean_depth = statistics.fmean(depths) if depths else 0.0
        std_depth = statistics.pstdev(depths) if len(depths) > 1 else 0.0
        cutoff = mean_depth + (0.5 - sensitivity) * std_depth
        chosen = {
            index
            for index in range(gap_count)
            if depths[index] > 0 and depths[index] >= cutoff
        }

    semantic_groups: list[list[int]] = []
    start = 0
    for index in range(gap_count):
        if index in chosen:
            semantic_groups.append(list(range(start, index + 1)))
            start = index + 1
    semantic_groups.append(list(range(start, len(units))))

    desired_scenes = requested_target or len(semantic_groups)
    final = _constrained_unit_partition(
        units,
        depths,
        chosen,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        desired_scenes=desired_scenes,
    )
    if not _is_exact_index_partition(final, len(units)):
        return build_scenes_from_segments(segments)

    if log_callback:
        log_callback(
            f"TextTiling: реплик={len(units)}, границ={len(final) - 1}, сцен={len(final)} "
            f"(sensitivity={sensitivity}, target={requested_target or 'auto'}, окно={block_window})"
        )

    scenes: list[Scene] = []
    for scene_index, group in enumerate(final, start=1):
        scene_segments = [
            segment for unit_index in group for segment in units[unit_index]
        ]
        scenes.append(make_scene_from_segments(scene_index, scene_segments))
    close_scene_timeline_gaps(scenes)
    if not _validate_scene_partition(scenes, segments):
        return build_scenes_from_segments(segments)
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


_FILM_MAP_SCENE_CHARS = 110


def _film_scene_map(scenes: list[Scene]) -> str:
    """One compact line per scene so a bounded batch still sees the whole film.

    Costs a few hundred tokens instead of the full transcript, which is what
    makes global awareness affordable inside a per-batch request.
    """

    lines = []
    for scene in scenes:
        gist = re.sub(r"\s+", " ", scene.voice_text or "").strip()
        if len(gist) > _FILM_MAP_SCENE_CHARS:
            gist = gist[:_FILM_MAP_SCENE_CHARS].rstrip() + "…"
        lines.append(f"  · {scene.index}. {scene.start}–{scene.end} | {gist}")
    return "\n".join(lines)


def _iter_scene_enrichment_batches(scenes: list[Scene]):
    """Yield requestable batches without splitting a scene's narration.

    A single narration longer than the hard request budget cannot satisfy both
    constraints.  It is deliberately omitted from LM calls and therefore keeps
    its existing deterministic labels; later scenes remain eligible.
    """

    batch: list[Scene] = []
    voice_chars = 0
    for scene in scenes:
        scene_chars = len(scene.voice_text or "")
        if scene_chars > _SCENE_ENRICHMENT_MAX_VOICE_CHARS:
            if batch:
                yield batch
                batch = []
                voice_chars = 0
            continue
        if batch and (
            len(batch) >= _SCENE_ENRICHMENT_MAX_SCENES
            or voice_chars + scene_chars > _SCENE_ENRICHMENT_MAX_VOICE_CHARS
        ):
            yield batch
            batch = []
            voice_chars = 0
        batch.append(scene)
        voice_chars += scene_chars
    if batch:
        yield batch


def _letter_script_counts(text: str) -> tuple[int, int]:
    """Return (Cyrillic, Latin) counts for a conservative language guard."""

    cyrillic = 0
    latin = 0
    for char in text or "":
        if not char.isalpha():
            continue
        codepoint = ord(char)
        if (
            0x0400 <= codepoint <= 0x052F
            or 0x1C80 <= codepoint <= 0x1C8F
            or 0x2DE0 <= codepoint <= 0x2DFF
            or 0xA640 <= codepoint <= 0xA69F
        ):
            cyrillic += 1
        elif (
            0x0041 <= codepoint <= 0x005A
            or 0x0061 <= codepoint <= 0x007A
            or 0x00C0 <= codepoint <= 0x024F
            or 0x1E00 <= codepoint <= 0x1EFF
        ):
            latin += 1
    return cyrillic, latin


def _dominant_letter_script(text: str, *, minimum_letters: int) -> str | None:
    cyrillic, latin = _letter_script_counts(text)
    total = cyrillic + latin
    if total < minimum_letters:
        return None
    if cyrillic / total >= 0.7:
        return "cyrillic"
    if latin / total >= 0.7:
        return "latin"
    return None


def _language_script_matches(source: str, candidate: str) -> bool:
    """Reject only an obvious Cyrillic/Latin translation mismatch.

    Numbers, very short text, mixed-script labels and short uppercase acronyms
    are intentionally accepted.  This is a script-family guard rather than a
    claim to distinguish, for example, Russian from Ukrainian.
    """

    source_script = _dominant_letter_script(source, minimum_letters=6)
    if source_script is None:
        return True
    letters = [char for char in candidate or "" if char.isalpha()]
    if len(letters) < 3:
        return True
    if len(letters) <= 6 and all(not char.islower() for char in letters):
        return True
    candidate_script = _dominant_letter_script(candidate, minimum_letters=3)
    return candidate_script is None or candidate_script == source_script


def _clamp_label_text(
    value: str,
    max_chars: int,
    *,
    max_words: int | None = None,
    reject_overflow: bool = False,
) -> str:
    """Apply the editorial length limits to one model-authored field.

    Shared by the structured and plain-JSON paths so a model that overshoots a
    limit degrades identically in both: the field is trimmed, or blanked when
    a truncated value would be meaningless (a half screen caption).
    """

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if max_words is not None:
        if reject_overflow and len(text.split()) > max_words:
            return ""
        text = " ".join(text.split()[:max_words])
    if len(text) > max_chars:
        if reject_overflow:
            return ""
        shortened = text[:max_chars].rstrip()
        if " " in shortened:
            shortened = shortened.rsplit(" ", 1)[0]
        text = shortened.rstrip(" ,.;:–—-")
    return text


def _apply_scene_enrichment_batch(
    batch: list[Scene], labels: list[SceneEnrichmentLabel]
) -> bool:
    """Validate identity and apply one response atomically to creative fields."""

    expected_indices = [scene.index for scene in batch]
    actual_indices = [label.index for label in labels]
    if (
        len(expected_indices) != len(set(expected_indices))
        or len(actual_indices) != len(set(actual_indices))
        or set(actual_indices) != set(expected_indices)
    ):
        return False

    by_index = {label.index: label for label in labels}
    prepared: list[tuple[Scene, tuple[str, str, str, str, str]]] = []
    for scene in batch:
        label = by_index[scene.index]
        title = _clamp_label_text(label.title, 120) or scene.title
        if not _language_script_matches(scene.voice_text, title):
            title = scene.title
        screen_text = normalize_numeric_screen_text(
            scene.voice_text,
            _clamp_label_text(
                label.screen_text,
                100,
                max_words=7,
                reject_overflow=True,
            ),
        )
        prepared.append(
            (
                scene,
                (
                    title,
                    _clamp_label_text(label.visual_direction, 2_000)
                    or scene.visual_direction,
                    screen_text,
                    _clamp_label_text(label.music_note, 500),
                    _clamp_label_text(label.retention_note, 500),
                ),
            )
        )

    previous = [
        (
            scene,
            (
                scene.title,
                scene.visual_direction,
                scene.screen_text,
                scene.music_note,
                scene.retention_note,
            ),
        )
        for scene in batch
    ]
    try:
        for scene, values in prepared:
            (
                scene.title,
                scene.visual_direction,
                scene.screen_text,
                scene.music_note,
                scene.retention_note,
            ) = values
    except Exception:
        for scene, values in previous:
            (
                scene.title,
                scene.visual_direction,
                scene.screen_text,
                scene.music_note,
                scene.retention_note,
            ) = values
        return False
    return True


def enrich_scenes_with_lm(
    scenes: list[Scene],
    base_url: str,
    model: str,
    ttl_seconds: int | None = None,
    style_anchor: str = "",
    character_description: str = "",
    niche: str = "documentary",
    video_style: str = DEFAULT_VIDEO_STYLE,
    log_callback=None,
    require_all: bool = False,
    allow_plain_json_fallback: bool = False,
) -> list[Scene]:
    """Fill creative fields in bounded, validated, batch-atomic LM calls."""
    if not scenes:
        return scenes
    del niche  # retained for compatibility with analyzer/plugin call sites

    style = get_video_style(video_style)
    style_block = "\n" + style_director_brief(video_style, style_anchor) + "\n"
    character_block = (
        f"ПЕРСОНАЖ (одинаков во всех сценах): {character_description}\n"
        if character_description
        else ""
    )
    style_guard = (
        "Для документального стиля запрещены вязание, пряжа, amigurumi, "
        "игрушечные и рукодельные материалы; показывай реальные локации "
        "и предметы как фотореалистичный документальный кадр.\n"
        if style.id == "documentary"
        else ""
    )
    lm_settings = SimpleNamespace(
        lm_url=base_url,
        lm_model=model.strip() or "local-model",
        lm_auto_unload=False,
        lm_unload_delay="0",
    )

    def report(message: str) -> None:
        if log_callback:
            log_callback(message)

    def short_error(exc: Exception) -> str:
        message = lm_client.explain_error(exc).replace("\r", " ").replace("\n", " ")
        message = re.sub(r"\s+", " ", message).strip()
        return message[:500] + ("..." if len(message) > 500 else "")

    def plain_scene_messages(scene: Scene) -> list[dict]:
        return [
            {
                "role": "system",
                "content": (
                    "Ты анализируешь РОВНО ОДНУ уже смонтированную сцену. "
                    "Верни один JSON-объект и ничего больше. Не разбивай сцену "
                    "на кадры, варианты или подсцены."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Стиль: {style.name}.\n"
                    f"{style_block}{character_block}"
                    "Проанализируй весь текст как ОДНУ монтажную сцену. "
                    "Нужна одна суммарная режиссёрская концепция, а не список кадров.\n"
                    "Верни РОВНО один JSON-объект с полями: index, title, "
                    "visual_direction, screen_text, music_note, retention_note.\n"
                    f"index обязан быть {scene.index}. Других объектов и массивов быть не должно.\n"
                    "screen_text по умолчанию оставляй пустой строкой. Заполняй его "
                    "ТОЛЬКО если в тексте сцены есть явная цифра, сумма, процент, дата "
                    "или измеримое значение; надпись обязана содержать ту же цифру "
                    "дословно, быть законченным фактом из 2–7 слов. Не используй "
                    "screen_text для заголовков, терминов или пересказа диктора.\n"
                    "title и непустой screen_text пиши на том же языке, что и текст сцены; "
                    "visual_direction можно писать по-английски для следующих "
                    "моделей генерации.\n"
                    f"{style_guard}"
                    f"Сцена {scene.index} | {scene.start}-{scene.end}\n"
                    f"Текст: {(scene.voice_text or '').strip()}"
                ),
            },
        ]

    def parse_plain_label(scene: Scene, content: str) -> SceneEnrichmentLabel:
        parsed = lm_client.extract_json(content)
        if not isinstance(parsed, dict):
            raise ValueError("LM Studio должна вернуть один JSON-объект")
        if "scenes" in parsed:
            values = parsed.get("scenes")
            if not isinstance(values, list) or len(values) != 1:
                raise ValueError(
                    "LM Studio разбила одну сцену на несколько объектов"
                )
            parsed = values[0]
        if not isinstance(parsed, dict):
            raise ValueError("LM Studio вернула неверный объект сцены")

        def bounded_text(
            field: str,
            max_chars: int,
            *,
            max_words: int | None = None,
            reject_overflow: bool = False,
        ) -> str:
            value = parsed.get(field, "")
            if not isinstance(value, str):
                raise ValueError(f"LM Studio вернула нестроковое поле {field}")
            text = re.sub(r"\s+", " ", value).strip()
            if max_words is not None:
                if reject_overflow and len(text.split()) > max_words:
                    return ""
                text = " ".join(text.split()[:max_words])
            if len(text) > max_chars:
                if reject_overflow:
                    return ""
                shortened = text[:max_chars].rstrip()
                if " " in shortened:
                    shortened = shortened.rsplit(" ", 1)[0]
                text = shortened.rstrip(" ,.;:–—-")
            return text

        normalized = {
            "index": scene.index,
            "title": bounded_text("title", 120),
            "visual_direction": bounded_text("visual_direction", 2000),
            "screen_text": bounded_text(
                "screen_text",
                100,
                max_words=7,
                reject_overflow=True,
            ),
            "music_note": bounded_text("music_note", 500),
            "retention_note": bounded_text("retention_note", 500),
        }
        # A one-scene request has an unambiguous identity. Some models locally
        # renumber it to 1; keep their creative work but restore the immutable
        # project index before strict field validation.
        return SceneEnrichmentLabel.model_validate(normalized)

    # Requests stay bounded, but every batch is told what the rest of the film
    # is about.  Without this each batch invents its own visual world and the
    # scene labels of a long video contradict each other, which is what forced
    # a second whole-film planning pass downstream.
    film_map = _film_scene_map(scenes)

    for batch in _iter_scene_enrichment_batches(scenes):
        compact = "\n".join(
            f"{scene.index}|{scene.start}-{scene.end}|{(scene.voice_text or '').strip()}"
            for scene in batch
        )
        film_block = (
            "КАРТА ВСЕГО РОЛИКА (для связности; озаглавливаешь только сцены ниже):\n"
            f"{film_map}\n\n"
            if film_map
            else ""
        )
        prompt = (
            f"Ты режиссёр монтажа YouTube-контента в стиле \"{style.name}\".\n\n"
            f"{film_block}"
            "Границы сцен УЖЕ заданы. НЕ объединяй и НЕ разбивай их, "
            "НЕ меняй index/тайминги/текст.\n"
            "Для КАЖДОЙ сцены по её тексту придумай поля:\n"
            "- title — короткое название сцены;\n"
            "- visual_direction — КОНКРЕТНОЕ описание сцены в выбранном стиле: "
            "локация, кто/что в кадре, ракурс, эмоция;\n"
            "- screen_text — ПО УМОЛЧАНИЮ пустая строка. Заполняй ТОЛЬКО для "
            "явной цифры/суммы/процента/даты/измеримого значения из voice_text; "
            "надпись обязана содержать ту же цифру дословно и быть законченным "
            "фактом из 2-7 слов. Не используй заголовки, термины и пересказ;\n"
            "- music_note — короткая заметка по музыке;\n"
            "- retention_note — заметка по удержанию.\n"
            f"{style_block}{character_block}"
            "title и непустой screen_text — на ТОМ ЖЕ ЯЗЫКЕ, что и voice_text; не переводи.\n"
            "Верни ТОЛЬКО JSON-объект {\"scenes\": [...]}. В массиве должен быть "
            "ровно один объект для каждого входного index и никаких других объектов.\n"
            "Поля объекта: index, title, visual_direction, screen_text, music_note, "
            "retention_note. Не возвращай start, end или voice_text.\n\n"
            "Сцены (index|start-end|текст):\n"
            f"{compact}"
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "Отвечай только валидным JSON по заданной схеме. Не добавляй markdown. "
                    "visual_direction — буквальная физическая сцена."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        indices = [scene.index for scene in batch]
        try:
            reply = lm_client.chat_structured(
                lm_settings,
                messages,
                SceneEnrichmentEnvelope,
                model=model.strip() or "local-model",
                transport="openai",
                temperature=0.3,
                max_tokens=4096,
                timeout=240,
                ttl=ttl_seconds,
                schema_name="scene_enrichment_batch",
            )
            envelope = reply.value
            if not isinstance(envelope, SceneEnrichmentEnvelope):
                raise ValueError(
                    f"unexpected structured value for scenes {indices}"
                )
            if not _apply_scene_enrichment_batch(batch, envelope.scenes):
                raise ValueError(
                    f"LM Studio returned non-exact scene indexes for batch {indices}"
                )
        except Exception as exc:
            strict_error = short_error(exc)
            logger.warning(
                "Scene enrichment failed for batch %s: %s",
                indices,
                strict_error,
            )
            if not allow_plain_json_fallback:
                if require_all:
                    raise RuntimeError(
                        f"LM Studio не проанализировала сцены {indices}: {strict_error}"
                    ) from exc
                continue

            report(
                f"LM Studio: strict JSON Schema недоступна для сцен {indices}; "
                "повторяю анализ обычным JSON."
            )
            try:
                plain_labels: list[SceneEnrichmentLabel] = []
                for scene in batch:
                    last_scene_error: Exception | None = None
                    for plain_attempt in range(2):
                        retry_messages = plain_scene_messages(scene)
                        if plain_attempt:
                            retry_messages[-1]["content"] += (
                                "\n\nПРЕДЫДУЩИЙ ОТВЕТ БЫЛ СИНТАКСИЧЕСКИ ПОВРЕЖДЁН. "
                                "Повтори с нуля: один компактный JSON-объект, двойные "
                                "кавычки у всех ключей и строк, без переносов внутри строк, "
                                "без markdown и комментариев."
                            )
                            report(
                                f"LM Studio: повторяю повреждённый JSON для сцены "
                                f"{scene.index} (2/2)."
                            )
                        try:
                            content = lm_client.chat(
                                lm_settings,
                                retry_messages,
                                temperature=0.2 if plain_attempt else 0.25,
                                timeout=240,
                                ttl=ttl_seconds,
                                max_tokens=1200,
                            )
                            plain_labels.append(parse_plain_label(scene, content))
                            last_scene_error = None
                            break
                        except Exception as scene_exc:
                            last_scene_error = scene_exc
                    if last_scene_error is not None:
                        raise last_scene_error
                if not _apply_scene_enrichment_batch(batch, plain_labels):
                    raise ValueError(
                        f"LM Studio returned non-exact scene indexes for batch {indices}"
                    )
                report(
                    f"LM Studio: сцены {indices[0]}–{indices[-1]} "
                    "успешно проанализированы через JSON fallback."
                )
            except Exception as fallback_exc:
                fallback_error = short_error(fallback_exc)
                logger.warning(
                    "Plain JSON scene enrichment failed for batch %s: %s",
                    indices,
                    fallback_error,
                )
                report(
                    f"LM Studio не смогла проанализировать сцены {indices}: "
                    f"{fallback_error}"
                )
                if require_all:
                    raise RuntimeError(
                        f"LM Studio не проанализировала сцены {indices}: "
                        f"{fallback_error}"
                    ) from fallback_exc
                continue
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
        headers=lm_client.request_headers(endpoint),
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
    if not segments:
        return []

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
        headers=lm_client.request_headers(endpoint),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        data = json.loads(response.read().decode("utf-8"))

    content = data["choices"][0]["message"]["content"].strip()
    raw_scenes = parse_lm_json_array(content, "LM Studio segment analysis")
    by_id = {index: segment for index, segment in enumerate(segments, start=1)}
    candidates: list[tuple[int, int, int, dict]] = []
    for raw_position, raw in enumerate(raw_scenes):
        if not isinstance(raw, dict):
            continue
        raw_ids = raw.get("segment_ids")
        if not isinstance(raw_ids, (list, tuple)):
            continue
        ids: list[int] = []
        for item in raw_ids:
            if isinstance(item, bool):
                continue
            try:
                segment_id = int(item)
            except (TypeError, ValueError):
                continue
            if segment_id in by_id:
                ids.append(segment_id)
        if not ids:
            continue
        unique_ids = sorted(set(ids))
        if unique_ids != list(range(unique_ids[0], unique_ids[-1] + 1)):
            # A distant hallucinated id must not widen a plausible scene over
            # hundreds of unrelated source segments. Skip the candidate; the
            # deterministic gap/tail fallback below will preserve all text.
            continue
        candidates.append((unique_ids[0], unique_ids[-1], raw_position, raw))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    scenes: list[Scene] = []
    cursor = 1

    def append_fallback(first_id: int, last_id: int) -> None:
        if first_id > last_id:
            return
        scenes.extend(build_scenes_from_segments(segments[first_id - 1 : last_id]))

    for first_id, last_id, _raw_position, raw in candidates:
        if last_id < cursor:
            continue
        if first_id > cursor:
            append_fallback(cursor, first_id - 1)
        first_id = max(first_id, cursor)
        selected = [by_id[item] for item in range(first_id, last_id + 1)]
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
        cursor = last_id + 1

    if cursor <= len(segments):
        append_fallback(cursor, len(segments))
    for index, scene in enumerate(scenes, start=1):
        scene.index = index

    result = _merge_similar_scenes(scenes)
    if not _validate_scene_partition(result, segments):
        return build_scenes_from_segments(segments)
    return result


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
