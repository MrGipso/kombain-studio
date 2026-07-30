import os
import re
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path


def legacy_asset_card_id(
    project_dir: Path | str,
    card_index: int,
    shot_index: int | str = "",
) -> str:
    """Return a repeatable UUID for a legacy board row missing ``card_id``.

    It remains stable across read-only loads and is written into the board on
    the next normal save.  Once persisted, moving a project does not change it.
    """
    project_key = os.path.normcase(str(Path(project_dir).resolve()))
    seed = f"{project_key}|asset-card|{int(card_index)}|shot:{shot_index}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


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
    # Short English ambient-sound phrase for the SFX generator (Woosh).
    sfx_query: str = ""
    # One-shot event sound for this shot (phone vibration, beep, knock):
    # played once at the shot start on top of the ambience, never looped.
    sfx_accent_query: str = ""
    # Reference-insert route: entity to resolve and preferred source.
    reference_query: str = ""
    reference_source: str = "wikipedia"
    # Deterministic semantic ownership.  The LLM proposes the visual concept;
    # the scheduler maps it onto immutable transcript segment IDs.
    segment_ids: list[int] = field(default_factory=list)
    visual_concept_id: str = ""
    # Structured story/continuity contract. These fields describe why the shot
    # exists and what must remain stable around it; provider prompts are merely
    # one rendering of this contract.
    purpose: str = ""
    continuity_mode: str = "independent"
    continuity_group_id: str = ""
    location_id: str = ""
    subject_ids: list[str] = field(default_factory=list)
    state_before: str = ""
    action: str = ""
    state_after: str = ""
    shot_size: str = ""
    camera_angle: str = ""
    screen_direction: str = ""
    reference_frame_id: str = ""
    transition_in: str = ""
    transition_out: str = ""
    change_from_previous: str = ""


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
    text_overlay_style: str = "subtitle"
    text_overlay_x: float = 50.0
    text_overlay_y: float = 86.0
    text_overlay_enter: str = "fade"
    text_overlay_exit: str = "fade"
    source_url: str = ""
    source_license: str = ""
    source_author: str = ""
    stock_score: float = 0.0
    # SFX layer (optional): ambient-sound query and the generated audio file.
    sfx_query: str = ""
    sfx_output: str = ""
    # Accent layer (optional): one-shot event sound at the shot start.
    sfx_accent_query: str = ""
    sfx_accent_output: str = ""
    # Shot importance from the LLM (high/medium/low): drives the LTX draft-mode
    # decision (hero shots keep the full two-pass render).
    priority: str = "medium"
    # Vision-QA score of the generated image (0..1; -1 = not checked).
    qa_score: float = -1.0
    # True when the current video_output was rendered with the fast draft LTX
    # workflow (so the board can flag it and offer a full "Финал" re-render).
    ltx_draft: bool = False
    # Reference-insert route (Wikipedia / arXiv): entity to look up + resolved
    # page/credit filled at generation time.
    reference_query: str = ""
    reference_source: str = "wikipedia"
    reference_url: str = ""
    reference_output: str = ""
    reference_credit: str = ""
    # Hash of the media-defining shot fields.  It prevents a regenerated shot
    # from inheriting outputs merely because it reused the same list index.
    plan_fingerprint: str = ""
    purpose: str = ""
    continuity_mode: str = "independent"
    continuity_group_id: str = ""
    location_id: str = ""
    subject_ids: list[str] = field(default_factory=list)
    state_before: str = ""
    action: str = ""
    state_after: str = ""
    shot_size: str = ""
    camera_angle: str = ""
    screen_direction: str = ""
    reference_frame_id: str = ""
    transition_in: str = ""
    transition_out: str = ""
    change_from_previous: str = ""
    # Stable identity survives board reordering and shot insertions.  Indices are
    # presentation/order metadata and must not be used as provenance keys.
    card_id: str = field(default_factory=lambda: str(uuid.uuid4()))


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


_NUMERIC_TOKEN_RE = re.compile(
    r"(?<![\w])(?:\d{1,3}(?:[ \u00a0.]\d{3})+|\d+)(?:[.,]\d+)?(?:\s*[%‰])?"
)
_DANGLING_SCREEN_TEXT_WORDS = {
    "а",
    "без",
    "благодаря",
    "в",
    "где",
    "для",
    "до",
    "за",
    "и",
    "из",
    "к",
    "как",
    "на",
    "над",
    "но",
    "о",
    "об",
    "от",
    "по",
    "под",
    "при",
    "про",
    "с",
    "со",
    "у",
    "через",
    "что",
    "это",
    "and",
    "at",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "the",
    "to",
    "with",
}


def _normalized_numeric_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in _NUMERIC_TOKEN_RE.finditer(text or ""):
        token = match.group(0).lower().replace("\u00a0", "").replace(" ", "")
        if token.endswith(("%", "‰")):
            suffix, token = token[-1], token[:-1]
        else:
            suffix = ""
        token = token.replace(",", ".")
        tokens.add(token + suffix)
    return tokens


def normalize_numeric_screen_text(
    source_text: str,
    candidate: str,
    *,
    max_words: int = 7,
    max_chars: int = 100,
) -> str:
    """Accept only a short, complete numeric fact supported by narration.

    On-screen copy is an accent layer, not a second subtitle track.  It is
    therefore empty unless the candidate contains an explicit number which is
    also present in the immutable narration text.  Overlong copy is rejected
    instead of mechanically truncated into an unfinished phrase.
    """

    value = re.sub(r"\s+", " ", str(candidate or "")).strip()
    if not value or len(value) > max_chars or not 1 <= len(value.split()) <= max_words:
        return ""
    source_numbers = _normalized_numeric_tokens(source_text)
    candidate_numbers = _normalized_numeric_tokens(value)
    if not candidate_numbers or not candidate_numbers.issubset(source_numbers):
        return ""
    last_word = value.rstrip(" .,!?:;…–—-").split()[-1].lower()
    if last_word in _DANGLING_SCREEN_TEXT_WORDS:
        return ""
    return value.rstrip(" .,!?:;…–—-")


def make_screen_text(text: str) -> str:
    """Create a conservative numeric label for the non-LM fallback path."""

    for sentence in re.split(r"(?<=[.!?])\s+", (text or "").replace("\n", " ")):
        words = re.sub(r"\s+", " ", sentence).strip(" .!?").split()
        number_index = next(
            (index for index, word in enumerate(words) if any(char.isdigit() for char in word)),
            None,
        )
        if number_index is None:
            continue
        candidate = " ".join(words[number_index : number_index + 4])
        normalized = normalize_numeric_screen_text(text, candidate)
        if normalized:
            return normalized
    return ""
