"""Persistence for the script stage (step 00).

The script lives in ``<project>/00_script.json``; the narration text that the
user records or sends to TTS is mirrored to ``00_script.txt``.  Every write
goes through :func:`core.script_workflow.migrate_script` so readers always get
the current schema.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.project import atomic_write_text
from core.script_workflow import (
    DEFAULT_WPM,
    assess_script_quality,
    estimate_duration_sec,
    migrate_script,
    split_into_beats,
    word_count,
)


SCRIPT_FILE = "00_script.json"
SCRIPT_TEXT_FILE = "00_script.txt"
SCRIPT_SOURCE_FILE = "00_script_source.txt"
SCRIPT_SOURCE_META_FILE = "00_script_source.json"
VOICE_PLAN_FILE = "00_voice_plan.json"


def read_script(pdir: Path) -> dict[str, Any]:
    p = pdir / SCRIPT_FILE
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return migrate_script(data)
        except (json.JSONDecodeError, OSError):
            pass
    return migrate_script(None)


def write_script(pdir: Path, data: dict[str, Any]) -> None:
    data = migrate_script(data)
    atomic_write_text(pdir / SCRIPT_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def write_source(pdir: Path, text: str, metadata: dict[str, Any]) -> None:
    atomic_write_text(pdir / SCRIPT_SOURCE_FILE, text)
    atomic_write_text(
        pdir / SCRIPT_SOURCE_META_FILE,
        json.dumps(metadata, ensure_ascii=False, indent=2),
    )


def _beats_from_generated(
    final_text: str,
    raw_beats: Any,
    *,
    target_seconds: int | None,
    wpm: int,
) -> list[dict[str, Any]]:
    metadata = raw_beats if isinstance(raw_beats, list) else []
    generated = [item for item in metadata if isinstance(item, dict) and str(item.get("text") or "").strip()]
    if generated and len(generated) == len(metadata):
        # One persisted beat per generated plan item. Flatten only the timing
        # skeleton; the authored paragraph formatting remains in beat.text.
        skeleton = "\n\n".join(re.sub(r"\s+", " ", str(item["text"])).strip() for item in generated)
        beats = split_into_beats(skeleton, target_seconds=target_seconds, wpm=wpm)
        for index, raw in enumerate(generated):
            beats[index]["title"] = str(raw.get("title") or beats[index]["title"]).strip()
            beats[index]["goal"] = str(raw.get("goal") or "").strip()
            beats[index]["visual_hint"] = str(raw.get("visual_hint") or "").strip()
            beats[index]["text"] = str(raw.get("text") or "").strip()
            beats[index]["content"] = str(raw.get("content") or "").strip()
            beats[index]["target_words"] = int(raw.get("target_words") or word_count(beats[index]["text"]))
        return beats

    beats = split_into_beats(final_text, target_seconds=target_seconds, wpm=wpm)
    for index, raw in enumerate(metadata[:len(beats)]):
        if not isinstance(raw, dict):
            continue
        beats[index]["title"] = str(raw.get("title") or beats[index]["title"]).strip()
        beats[index]["goal"] = str(raw.get("goal") or "").strip()
        beats[index]["visual_hint"] = str(raw.get("visual_hint") or "").strip()
    return beats


def store_processed_script(
    pdir: Path,
    script: dict[str, Any],
    *,
    source: str,
    final_text: str,
    analysis: dict[str, Any],
    target_seconds: int | None,
    raw_beats: Any = None,
) -> dict[str, Any]:
    """Persist a usable draft before any optional length-correction pass."""
    wpm = int(script.get("estimated_wpm") or DEFAULT_WPM)
    beats = _beats_from_generated(final_text, raw_beats, target_seconds=target_seconds, wpm=wpm)

    previous_final_text = str(script.get("final_text") or "")
    text_changed = final_text != previous_final_text
    script.update({
        "cleaned_text": source,
        "final_text": final_text,
        "analysis": analysis,
        "beats": beats,
        "estimated_duration_sec": estimate_duration_sec(final_text, wpm),
        "text_revision": int(script.get("text_revision", 0) or 0) + (1 if text_changed else 0),
        "voice_outdated": bool(script.get("actual_audio_duration_sec") is not None and text_changed),
        "voice_plan": {"status": "empty", "chunks": []},
        "revision": int(script.get("revision", 0) or 0) + 1,
    })
    script["quality"] = assess_script_quality(script)
    write_script(pdir, script)
    atomic_write_text(pdir / SCRIPT_TEXT_FILE, final_text)
    return script


__all__ = [
    "SCRIPT_FILE",
    "SCRIPT_SOURCE_FILE",
    "SCRIPT_SOURCE_META_FILE",
    "SCRIPT_TEXT_FILE",
    "VOICE_PLAN_FILE",
    "read_script",
    "store_processed_script",
    "write_script",
    "write_source",
]
