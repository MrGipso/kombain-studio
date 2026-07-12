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


def load_script_beats(project_dir: Path) -> list[dict[str, Any]] | None:
    """Beats with non-empty narration text, or None if the script isn't usable."""
    path = Path(project_dir) / "00_script.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    beats = [b for b in data.get("beats", []) if isinstance(b, dict) and (b.get("text") or "").strip()]
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
    if n_seg == 0:
        return None
    seg_norm = [_norm(getattr(s, "text", "") or "") for s in segments]

    total_words = sum(len(_norm(b["text"]).split()) for b in beats) or 1
    boundaries = [0]
    failed = 0
    prev = 0
    for bi in range(1, len(beats)):
        anchor = " ".join(_norm(beats[bi]["text"]).split()[:ANCHOR_WORDS])
        best_idx, best_ratio = None, 0.0
        # Roll a 2-segment window so an anchor split across segments still matches.
        for i in range(prev + 1, n_seg):
            window = seg_norm[i] if i + 1 >= n_seg else seg_norm[i] + " " + seg_norm[i + 1]
            r = _ratio(anchor, " ".join(window.split()[:ANCHOR_WORDS + 4]))
            if r > best_ratio:
                best_idx, best_ratio = i, r
        if best_idx is None or best_ratio < MIN_BOUNDARY_RATIO:
            failed += 1
            # Proportional fallback for this one boundary: place it by the
            # beat's share of total scripted words.
            done_words = sum(len(_norm(b["text"]).split()) for b in beats[:bi])
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
    return list(zip(boundaries[:-1], boundaries[1:]))


def scenes_from_script(project_dir: Path, segments: list[Any], scene_cls) -> list[Any] | None:
    """Build Scene objects from authored beats aligned to the transcript.

    `scene_cls` is the caller's Scene dataclass (the monolith and core each have
    one). Returns None when the script is absent or alignment fails — the
    caller then falls back to embedding segmentation.
    """
    beats = load_script_beats(project_dir)
    if not beats:
        return None
    ranges = align_beats_to_segments(beats, segments)
    if not ranges:
        return None
    scenes = []
    for idx, (beat, (s0, s1)) in enumerate(zip(beats, ranges), start=1):
        segs = segments[s0:s1]
        if not segs:
            return None
        scenes.append(scene_cls(
            index=idx,
            start=getattr(segs[0], "start", "00:00:00"),
            end=getattr(segs[-1], "end", "00:00:00"),
            title=beat.get("title") or f"Бит {idx}",
            voice_text=" ".join((getattr(s, "text", "") or "").strip() for s in segs),
            visual_direction=(beat.get("visual_hint") or beat.get("goal") or ""),
            screen_text="",
            music_note="",
            retention_note=beat.get("goal") or "",
        ))
    return scenes
