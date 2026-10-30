"""Authored scenes: align script beats onto the recorded voice-over.

When a project has a script (00_script.json with beat texts), scene boundaries
should come from the AUTHOR's beats, not from embedding statistics: the
narration was literally written as those beats. This module finds, for each
beat boundary, the transcript segment where the next beat's text begins (fuzzy
matching — the narrator may deviate slightly from the written text) and builds
Scene objects with the beat's title/goal/visual_hint already filled in.

Falls back to None (caller uses embedding segmentation) when there is no
usable script or the match confidence is too low.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

MIN_BOUNDARY_RATIO = 0.5  # below this the anchor match is considered failed
ANCHOR_WORDS = 12


def _norm(text: str) -> str:
    text = re.sub(r"[^\w\s]", " ", (text or "").lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _prefix_ratio(anchor: str, text: str) -> float:
    """Compare an anchor with the beginning of a transcript window.

    Using the anchor's own word count prevents short beat titles/narration
    openings from being penalised merely because a transcript segment also
    contains the following sentence.
    """
    anchor_words = anchor.split()
    text_words = text.split()
    if not anchor_words or not text_words:
        return 0.0
    sizes = {
        max(1, len(anchor_words) - 2),
        len(anchor_words),
        min(len(text_words), len(anchor_words) + 4),
    }
    return max(
        _ratio(anchor, " ".join(text_words[:size]))
        for size in sizes
        if size <= len(text_words)
    )


def _beat_text(beat: Any) -> str:
    if not isinstance(beat, dict):
        return ""
    value = beat.get("text")
    return value.strip() if isinstance(value, str) else ""


def load_script_beats(project_dir: Path) -> list[dict[str, Any]] | None:
    """Beats with non-empty narration text, or None if the script isn't usable."""
    path = Path(project_dir) / "00_script.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    raw_beats = data.get("beats", [])
    if not isinstance(raw_beats, list):
        return None
    beats = [beat for beat in raw_beats if _beat_text(beat)]
    # Require the script to actually cover the video: at least 3 authored beats.
    return beats if len(beats) >= 3 else None


def align_beats_to_segments(beats: list[dict[str, Any]], segments: list[Any]) -> list[tuple[int, int]] | None:
    """Map each beat to a [start_segment, end_segment) range.

    Strategy: for each boundary between beat i and beat i+1, take the first
    ~ANCHOR_WORDS words of beat i+1 as an anchor phrase and find the segment
    (after the previous boundary) whose rolling window of text best matches it.
    Returns None when too many anchors fail (narration diverged from script).
    """
    n_seg = len(segments)
    if n_seg == 0 or len(beats) < 1:
        return None
    seg_norm = [_norm(getattr(s, "text", "") or "") for s in segments]
    beat_texts = [_norm(_beat_text(beat)) for beat in beats]
    if any(not text for text in beat_texts):
        return None

    total_words = sum(len(text.split()) for text in beat_texts) or 1
    boundaries = [0]
    failed = 0
    prev = 0
    for bi in range(1, len(beats)):
        anchor = " ".join(beat_texts[bi].split()[:ANCHOR_WORDS])
        best_idx, best_ratio = None, 0.0
        # Roll a 2-segment window so an anchor split across segments still matches.
        for i in range(prev + 1, n_seg):
            window = seg_norm[i] if i + 1 >= n_seg else seg_norm[i] + " " + seg_norm[i + 1]
            r = _prefix_ratio(anchor, window)
            if r > best_ratio:
                best_idx, best_ratio = i, r
        if best_idx is None or best_ratio < MIN_BOUNDARY_RATIO:
            failed += 1
            # Proportional fallback for this one boundary: place it by the
            # beat's share of total scripted words.
            done_words = sum(len(text.split()) for text in beat_texts[:bi])
            best_idx = max(prev + 1, min(n_seg - 1, round(n_seg * done_words / total_words)))
        boundaries.append(best_idx)
        prev = best_idx
    boundaries.append(n_seg)

    # Too many fuzzy failures = the recording doesn't follow the script.
    if failed > max(1, len(beats) // 3):
        return None
    # Boundaries must be strictly increasing to form valid ranges.
    for a, b in zip(boundaries, boundaries[1:]):
        if b <= a:
            return None

    # Boundary anchors validate beats 2..N.  Validate the first beat as well:
    # otherwise a different script with only a matching tail can be accepted.
    first_anchor = " ".join(beat_texts[0].split()[:ANCHOR_WORDS])
    first_end = boundaries[1]
    first_ratio = 0.0
    for index in range(0, first_end):
        window = seg_norm[index]
        if index + 1 < first_end:
            window += " " + seg_norm[index + 1]
        first_ratio = max(first_ratio, _prefix_ratio(first_anchor, window))
    if first_ratio < MIN_BOUNDARY_RATIO:
        return None
    return list(zip(boundaries[:-1], boundaries[1:]))


def scenes_from_script(
    project_dir: Path,
    segments: list[Any],
    scene_cls,
    min_seconds: int = 20,
    max_seconds: int = 90,
    target_seconds: int = 45,
    partitioner: Callable[..., list[list[Any]]] | None = None,
) -> list[Any] | None:
    """Build edit-sized Scene objects inside authored transcript chapters.

    `scene_cls` is the Scene dataclass to instantiate (normally
    core.models.Scene). Authored beats define immutable chapter ranges; long chapters are
    partitioned on real transcript boundaries so they cannot become multi-
    minute editing scenes. Returns None when the script is absent or alignment
    fails — the caller then falls back to embedding segmentation.

    `partitioner` chooses where the cuts inside a chapter land. It defaults to
    the duration partitioner; callers with an embedding endpoint pass the
    semantic one so an authored chapter is still cut on topic shifts rather
    than every `target_seconds`. Chapter boundaries stay authoritative either
    way — the partitioner is only ever handed one chapter at a time.
    """
    from core.scenes import close_scene_timeline_gaps, partition_segments_by_duration

    split = partitioner or partition_segments_by_duration

    beats = load_script_beats(project_dir)
    if not beats:
        return None
    ranges = align_beats_to_segments(beats, segments)
    if not ranges:
        return None
    scenes = []
    for beat_index, (beat, (s0, s1)) in enumerate(zip(beats, ranges), start=1):
        segs = segments[s0:s1]
        if not segs:
            return None
        groups = split(
            segs,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            target_seconds=target_seconds,
        )
        # A partitioner is allowed to degrade, never to lose narration.
        if sum(len(group) for group in groups) != len(segs):
            groups = partition_segments_by_duration(
                segs,
                min_seconds=min_seconds,
                max_seconds=max_seconds,
                target_seconds=target_seconds,
            )
        base_title = beat.get("title") or f"Глава {beat_index}"
        for part_index, part in enumerate(groups, start=1):
            title = (
                base_title
                if len(groups) == 1
                else f"{base_title} · {part_index}/{len(groups)}"
            )
            scenes.append(scene_cls(
                index=len(scenes) + 1,
                start=getattr(part[0], "start", "00:00:00"),
                end=getattr(part[-1], "end", "00:00:00"),
                title=title,
                voice_text=" ".join(
                    (getattr(segment, "text", "") or "").strip()
                    for segment in part
                ),
                visual_direction=(beat.get("visual_hint") or beat.get("goal") or ""),
                screen_text="",
                music_note="",
                retention_note=beat.get("goal") or "",
            ))
    return close_scene_timeline_gaps(scenes)
