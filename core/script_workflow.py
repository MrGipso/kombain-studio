"""Canonical script intake, subtitle cleanup and timing helpers."""

from __future__ import annotations

import html
import re
import uuid
from typing import Any


SCRIPT_SCHEMA_VERSION = 2
DEFAULT_WPM = 135
MIN_REWRITE_WORD_RATIO = 0.95
MAX_REWRITE_WORD_RATIO = 1.05
REVIEW_MIN_REWRITE_WORD_RATIO = 0.85
REVIEW_MAX_REWRITE_WORD_RATIO = 1.15

# OmniVoice 0.1.5 understands these literal non-verbal tokens.  They are
# deliberately kept separate from our provider-neutral emotion labels.
OMNIVOICE_NONVERBAL_TAGS = frozenset({
    "[laughter]", "[sigh]", "[confirmation-en]", "[question-en]",
    "[question-ah]", "[question-oh]", "[question-ei]", "[question-yi]",
    "[surprise-ah]", "[surprise-oh]", "[surprise-wa]", "[surprise-yo]",
    "[dissatisfaction-hnn]",
})


def clean_subtitle_text(text: str) -> str:
    """Convert SRT/VTT/plain captions into readable source text.

    The operation is deliberately deterministic.  Semantic cleanup remains a
    separate LM-assisted review step, so importing can never silently rewrite
    the user's source.
    """
    value = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"^WEBVTT[^\n]*\n", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^\s*(?:Kind|Language):.*$", "", value, flags=re.MULTILINE | re.IGNORECASE)
    value = re.sub(r"^\s*(?:NOTE|STYLE|REGION)\b.*?(?=\n\s*\n|\Z)", "", value, flags=re.MULTILINE | re.DOTALL)
    value = re.sub(
        r"^\s*\d{1,3}:\d{2}(?::\d{2})?[.,]\d{3}\s*-->\s*"
        r"\d{1,3}:\d{2}(?::\d{2})?[.,]\d{3}.*$",
        "",
        value,
        flags=re.MULTILINE,
    )
    value = re.sub(r"^\s*\d+\s*$", "", value, flags=re.MULTILINE)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(
        r"\[(?:музыка|music|аплодисменты|applause|смех|laughter|шум|noise)\]",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = html.unescape(value)

    lines: list[str] = []
    previous = ""
    for raw in value.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        line = re.sub(r"^>+\s*", "", line).strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        # Auto captions often repeat the same rolling line verbatim.
        if line.casefold() == previous.casefold():
            continue
        lines.append(line)
        previous = line

    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(p.strip() for p in paragraphs if p.strip()).strip()


def word_count(text: str) -> int:
    return len(re.findall(r"[\wЁёА-Яа-я-]+", text, flags=re.UNICODE))


def estimate_duration_sec(text: str, wpm: int | float = DEFAULT_WPM) -> int:
    """Estimate narration duration including conservative punctuation pauses."""
    safe_wpm = max(60.0, min(240.0, float(wpm or DEFAULT_WPM)))
    speech = word_count(text) / safe_wpm * 60.0
    sentence_pauses = len(re.findall(r"[.!?…](?:[\"'»)]|\s|$)", text)) * 0.30
    paragraph_pauses = max(0, len(re.findall(r"\n\s*\n", text))) * 0.55
    comma_pauses = len(re.findall(r"[,;:]", text)) * 0.10
    return max(0, round(speech + sentence_pauses + paragraph_pauses + comma_pauses))


def split_into_beats(text: str, *, target_seconds: int | None = None, wpm: int = DEFAULT_WPM) -> list[dict[str, Any]]:
    """Create stable, editable beats without asking the model to invent content."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs and text.strip():
        paragraphs = [text.strip()]
    if len(paragraphs) == 1 and word_count(text) > 220:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]
        paragraphs = [" ".join(sentences[i:i + 5]) for i in range(0, len(sentences), 5)]

    estimates = [max(1, estimate_duration_sec(p, wpm)) for p in paragraphs]
    if target_seconds and estimates:
        scale = max(0.2, float(target_seconds) / sum(estimates))
        estimates = [max(5, round(value * scale)) for value in estimates]

    return [
        {
            "id": f"beat_{index + 1:02d}_{uuid.uuid4().hex[:6]}",
            "title": f"Блок {index + 1}",
            "goal": "",
            "duration_sec": estimates[index],
            "target_words": word_count(paragraph),
            "visual_hint": "",
            "content": "",
            "text": paragraph,
        }
        for index, paragraph in enumerate(paragraphs)
    ]


def default_script() -> dict[str, Any]:
    return {
        "schema_version": SCRIPT_SCHEMA_VERSION,
        "mode": "ready",
        "source": {"type": "text", "name": "", "url": "", "language": "ru", "caption_kind": ""},
        "source_text": "",
        "cleaned_text": "",
        "final_text": "",
        "target_duration_sec": None,
        "target_duration_min_sec": None,
        "target_duration_max_sec": None,
        "duration_mode": "auto",
        "duration_tolerance_pct": 5,
        "speech_style": "normal",
        "estimated_wpm": DEFAULT_WPM,
        "estimated_duration_sec": 0,
        "source_estimated_duration_sec": 0,
        "actual_audio_duration_sec": None,
        "voice_profile_id": "",
        "text_revision": 0,
        "voice_source_revision": None,
        "voice_outdated": False,
        "revision": 0,
        "analysis": {"facts": [], "theses": [], "warnings": []},
        "beats": [],
        "voice_plan": {"status": "empty", "chunks": []},
        "agent": {
            "status": "idle",
            "stage": "",
            "progress": 0,
            "iteration": 0,
            "max_iterations": 1,
            "score": 0,
            "issues": [],
            "history": [],
            "last_report_signature": "",
            "last_checked_revision": -1,
        },
        "quality": {
            "status": "empty",
            "target_words": 0,
            "actual_words": 0,
            "word_ratio": 0.0,
            "total_beats": 0,
            "completed_beats": 0,
            "coverage_ratio": 0.0,
            "accepted_actual": False,
        },
        # Legacy keys stay readable while older saved projects are migrated.
        "topic": "",
        "target_minutes": 8,
        "audience_notes": "",
        "facts": "",
        "hooks": [],
        "hook_selected": 0,
    }


def migrate_script(data: dict[str, Any] | None) -> dict[str, Any]:
    result = default_script()
    if isinstance(data, dict):
        result.update(data)
    result["schema_version"] = SCRIPT_SCHEMA_VERSION
    if result.get("mode") not in {"ready", "rewrite"}:
        result["mode"] = "ready"
    if result.get("mode") == "rewrite" and result.get("target_duration_sec"):
        result["target_minutes"] = max(1, round(int(result["target_duration_sec"]) / 60))
    beats = result.get("beats") if isinstance(result.get("beats"), list) else []
    for index, beat in enumerate(beats):
        if isinstance(beat, dict):
            beat.setdefault("id", f"beat_{index + 1:02d}_{uuid.uuid4().hex[:6]}")
            beat.setdefault("target_words", word_count(str(beat.get("text", ""))))
    result["beats"] = beats
    default_agent = default_script()["agent"]
    existing_agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
    result["agent"] = {**default_agent, **existing_agent}

    if not result.get("final_text") and beats:
        hooks = result.get("hooks") or []
        selected = int(result.get("hook_selected", 0) or 0)
        parts = [str(hooks[min(selected, len(hooks) - 1)]).strip()] if hooks else []
        parts.extend(str(beat.get("text", "")).strip() for beat in beats if str(beat.get("text", "")).strip())
        result["final_text"] = "\n\n".join(part for part in parts if part)
    result["text_revision"] = max(0, int(result.get("text_revision") or 0))
    voice_source_revision = result.get("voice_source_revision")
    result["voice_source_revision"] = (
        max(0, int(voice_source_revision)) if voice_source_revision is not None else None
    )
    result["voice_outdated"] = bool(result.get("voice_outdated", False))
    result["estimated_duration_sec"] = estimate_duration_sec(
        str(result.get("final_text", "")), int(result.get("estimated_wpm", DEFAULT_WPM) or DEFAULT_WPM)
    )
    if not int(result.get("source_estimated_duration_sec") or 0):
        result["source_estimated_duration_sec"] = estimate_duration_sec(
            str(result.get("source_text") or ""), int(result.get("estimated_wpm", DEFAULT_WPM) or DEFAULT_WPM)
        )
    result["quality"] = assess_script_quality(result)
    return result


def assess_script_quality(script: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic readiness metrics for script-to-voice handoff."""
    existing = script.get("quality") if isinstance(script.get("quality"), dict) else {}
    actual_words = word_count(str(script.get("final_text") or ""))
    mode = str(script.get("mode") or "ready")
    if mode != "rewrite":
        return {
            "status": "ready" if actual_words else "empty",
            "target_words": actual_words,
            "actual_words": actual_words,
            "word_ratio": 1.0 if actual_words else 0.0,
            "total_beats": len(script.get("beats") or []),
            "completed_beats": len(script.get("beats") or []) if actual_words else 0,
            "coverage_ratio": 1.0 if actual_words else 0.0,
            "accepted_actual": bool(existing.get("accepted_actual", False)),
        }

    target_words = int(existing.get("target_words") or 0)
    if target_words <= 0:
        target_seconds = int(script.get("target_duration_sec") or 0)
        wpm = int(script.get("estimated_wpm") or DEFAULT_WPM)
        target_words = max(100, round(target_seconds / 60 * wpm * 0.90)) if target_seconds else 0
    total_beats = max(0, int(existing.get("total_beats") or len(script.get("beats") or [])))
    completed_beats = max(0, min(total_beats, int(existing.get("completed_beats") or 0)))
    if not total_beats and actual_words:
        total_beats = len(script.get("beats") or [])
        completed_beats = total_beats
    coverage_ratio = completed_beats / total_beats if total_beats else (1.0 if actual_words else 0.0)
    word_ratio = actual_words / target_words if target_words else 0.0
    complete_coverage = coverage_ratio >= 0.999
    if not actual_words:
        status = "empty"
    elif complete_coverage and MIN_REWRITE_WORD_RATIO <= word_ratio <= MAX_REWRITE_WORD_RATIO:
        status = "ready"
    elif complete_coverage and REVIEW_MIN_REWRITE_WORD_RATIO <= word_ratio <= REVIEW_MAX_REWRITE_WORD_RATIO:
        status = "review"
    else:
        status = "incomplete"
    return {
        "status": status,
        "target_words": target_words,
        "actual_words": actual_words,
        "word_ratio": round(word_ratio, 4),
        "total_beats": total_beats,
        "completed_beats": completed_beats,
        "coverage_ratio": round(coverage_ratio, 4),
        "accepted_actual": bool(existing.get("accepted_actual", False)),
    }


def _voice_direction(sentence: str) -> tuple[str, float, float, int]:
    """Return emotion, intensity, speed multiplier and base ending pause."""
    lowered = sentence.casefold()
    if sentence.rstrip().endswith("?"):
        return "curiosity", 0.62, 0.97, 700
    if re.search(r"\b(?:опасност|риск|угроз|авари|катастроф|критическ|проблем)\w*", lowered):
        return "tension", 0.68, 0.94, 520
    if re.search(r"\b(?:итак|таким образом|в итоге|главный вывод|поэтому)\b", lowered):
        return "confident", 0.60, 0.98, 600
    if sentence.rstrip().endswith("!") or re.search(r"\b(?:важно|ключев|именно|никогда|всегда)\w*", lowered):
        return "emphasis", 0.68, 1.03, 620
    if re.search(r"\b(?:неожидан|удивитель|поразитель|внезапн)\w*", lowered):
        return "surprise", 0.60, 1.02, 620
    return "neutral", 0.45, 1.0, 350


def build_voice_plan(
    text: str,
    *,
    speed: float = 1.0,
    pause_scale: float = 1.0,
) -> dict[str, Any]:
    """Build provider-neutral chunks; the OmniVoice adapter compiles them later."""
    safe_speed = max(0.7, min(1.3, float(speed)))
    safe_pause_scale = max(0.25, min(3.0, float(pause_scale)))
    chunks: list[dict[str, Any]] = []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for paragraph_index, paragraph in enumerate(paragraphs):
        sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", paragraph) if s.strip()]
        for sentence in sentences:
            emotion, intensity, speed_factor, pause = _voice_direction(sentence)
            if sentence.endswith("…"):
                pause = max(pause, 700)
            tags = [tag for tag in re.findall(r"\[[a-z-]+\]", sentence.casefold()) if tag in OMNIVOICE_NONVERBAL_TAGS]
            chunks.append({
                "id": f"chunk_{len(chunks) + 1:04d}",
                "text": sentence,
                "emotion": emotion,
                "intensity": intensity,
                "speed": round(max(0.7, min(1.3, safe_speed * speed_factor)), 3),
                "pause_before_ms": 0,
                "pause_after_ms": round(pause * safe_pause_scale),
                "provider_tags": tags,
            })
        if chunks and paragraph_index < len(paragraphs) - 1:
            chunks[-1]["pause_after_ms"] = max(
                chunks[-1]["pause_after_ms"], round(750 * safe_pause_scale)
            )
    return {
        "status": "ready" if chunks else "empty",
        "settings": {"speed": safe_speed, "pause_scale": safe_pause_scale},
        "chunks": chunks,
    }
