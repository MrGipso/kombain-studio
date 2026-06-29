import re
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Scene:
    index: int
    start: str
    end: str
    title: str
    voice_text: str
    visual_direction: str
    screen_text: str
    music_note: str
    retention_note: str


@dataclass
class TranscriptSegment:
    start: str
    end: str
    text: str


@dataclass
class Shot:
    index: int
    scene_index: int
    start: str
    end: str
    visual_type: str
    description: str
    asset_query: str
    image_model: str
    video_model: str
    image_prompt: str
    video_prompt: str
    screen_text: str
    motion: str
    priority: str
    fallback: str


@dataclass
class AssetCard:
    shot_index: int
    scene_index: int
    scene_title: str
    start: str
    end: str
    visual_type: str
    route: str
    description: str
    asset_query: str
    image_model: str
    video_model: str
    remotion_template: str
    image_prompt: str
    video_prompt: str
    screen_text: str
    motion: str
    fallback: str
    image_output: str
    video_output: str
    remotion_output: str
    status: str
    notes: str
    approved: bool
    timeline_start: str = ""
    timeline_end: str = ""
    assigned_media: str = ""
    track: str = "video_1"
    text_overlay_enabled: bool = False
    text_overlay_text: str = ""
    text_overlay_position: str = "bottom"


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\wа-яё]+", "-", text, flags=re.IGNORECASE)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "content-project"


def clean_script_text(text: str) -> str:
    text = re.sub(
        r"\[(?:ПАУЗА(?: 1 СЕК)?|АКЦЕНТ|ТИШЕ|БЫСТРЕЕ|МЕДЛЕННЕЕ)\]",
        "", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"^#.+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^-{3,}$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def seconds_to_timecode(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def seconds_to_srt_time(seconds: int) -> str:
    return seconds_to_timecode(seconds) + ",000"


def timecode_to_seconds(timecode: str) -> int:
    parts = str(timecode).split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(float(parts[1]))
    return int(float(timecode)) if timecode else 0


def estimate_duration_seconds(text: str) -> int:
    words = len(text.split())
    return max(12, round(words / 2.25))


def split_into_blocks(text: str, max_words: int = 95) -> list[str]:
    text = clean_script_text(text)
    paragraphs = [
        p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()
    ]
    blocks: list[str] = []
    current: list[str] = []
    current_words = 0

    for paragraph in paragraphs:
        words = len(paragraph.split())
        if current and current_words + words > max_words:
            blocks.append("\n\n".join(current))
            current = []
            current_words = 0
        current.append(paragraph)
        current_words += words

    if current:
        blocks.append("\n\n".join(current))

    return blocks


def normalize_timestamp(value) -> str:
    if isinstance(value, (int, float)):
        return seconds_to_timecode(round(float(value)))
    text = str(value).strip()
    if "," in text:
        text = text.split(",", 1)[0]
    if "." in text:
        text = text.split(".", 1)[0]
    parts = text.split(":")
    if len(parts) == 3:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(float(parts[2])):02d}"
    if len(parts) == 2:
        return f"00:{int(parts[0]):02d}:{int(float(parts[1])):02d}"
    if text.isdigit():
        return seconds_to_timecode(int(text))
    return "00:00:00"


def make_title(text: str, index: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " ").strip())
    first = sentences[0] if sentences else f"Scene {index}"
    first = re.sub(r"\s+", " ", first).strip()
    if len(first) > 58:
        first = first[:55].rsplit(" ", 1)[0] + "..."
    return first or f"Scene {index}"


def make_screen_text(text: str) -> str:
    candidates = re.findall(r"[^.!?]+[.!?]?", text.replace("\n", " "))
    candidates = [
        re.sub(r"\s+", " ", item).strip(" .!?")
        for item in candidates if len(item.split()) >= 4
    ]
    if not candidates:
        return ""
    candidate = max(candidates[:5], key=len)
    if len(candidate) > 54:
        candidate = candidate[:51].rsplit(" ", 1)[0] + "..."
    return candidate
