import csv
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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


AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
SETTINGS_PATH = Path(__file__).resolve().parent / "content_pipeline_settings.json"
DEFAULT_COMFY_KREA_WORKFLOW = Path(r"H:\image_krea2_turbo_t2i_api.json")
DEFAULT_COMFY_IDEOGRAM_WORKFLOW = Path(r"H:\image_ideogram4_t2i_api.json")
DEFAULT_COMFY_LTX_WORKFLOW = Path(r"H:\Animal\video-generation\LTX23_GGUF_i2v_api.json")
COMFY_KREA_WORKFLOW = DEFAULT_COMFY_KREA_WORKFLOW
COMFY_IDEOGRAM_WORKFLOW = DEFAULT_COMFY_IDEOGRAM_WORKFLOW
COMFY_LTX_WORKFLOW = DEFAULT_COMFY_LTX_WORKFLOW
SHOT_TARGET_SECONDS = 4
SHOT_MAX_SECONDS = 40
MAX_LTX_DURATION = 40

APP_BG = "#f3f6fb"
PANEL_BG = "#ffffff"
SIDEBAR_BG = "#101827"
SIDEBAR_FG = "#d8e2f0"
TEXT_FG = "#172033"
MUTED_FG = "#657083"
ACCENT = "#2f7cf6"
ACCENT_DARK = "#1d5fd0"
BORDER = "#d8e0ec"


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


def build_heuristic_shots(scenes: list[Scene], image_preference: str = "Auto", style_anchor: str = "") -> list[Shot]:
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
            route = choose_visual_route(text_hint, scene.screen_text, image_preference=image_preference)
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
    return shots


def choose_visual_route(text: str, screen_text: str = "", image_preference: str = "Auto", style_anchor: str = "") -> dict:
    lower = f"{text} {screen_text}".lower()
    preferred_image_model = preferred_realistic_image_model(image_preference)

    if screen_text and any(word in lower for word in ("схема", "bgp", "маршрут", "обход")):
        return {
            "visual_type": "motion_text",
            "asset_query": "documentary motion typography overlay",
            "image_model": "",
            "video_model": "",
            "image_prompt": "",
            "video_prompt": "",
            "motion": "Remotion text reveal, subtle scale-in, clean documentary typography",
            "fallback": "Remotion motion_text overlay",
        }

    if any(word in lower for word in ("схема", "bgp", "маршрут", "обход", "chokepoint")):
        return {
            "visual_type": "remotion_graphic",
            "asset_query": "schema infographic network route",
            "image_model": "",
            "video_model": "",
            "image_prompt": "",
            "video_prompt": "",
            "motion": "animate lines and labels",
            "fallback": "Remotion graphic or SVG schema",
        }

    prompt = make_realistic_prompt(text, style_anchor=style_anchor)
    video_prompt = make_ltx_prompt(prompt)
    return {
        "visual_type": "ai_image_then_i2v",
        "asset_query": make_asset_query(lower),
        "image_model": preferred_image_model,
        "video_model": "ltx2.3_i2v",
        "image_prompt": prompt,
        "video_prompt": video_prompt,
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
    hint = clean_visual_prompt_source(text)
    if len(hint) > 300:
        hint = hint[:297].rsplit(" ", 1)[0] + "..."
    shot = shot_type or _suggest_shot_type(hint)
    anchor_suffix = f" Style: {style_anchor}." if style_anchor else ""
    templates = [
        f"Documentary editorial photograph, {shot}, natural lighting, "
        f"rich textures and materials, sharp focus, ambient environment. Scene: {hint}.{anchor_suffix}",
        f"Cinematic documentary frame, {shot}, carefully composed with "
        f"natural color palette, diffused lighting, documentary aesthetic. Scene: {hint}.{anchor_suffix}",
        f"Photorealistic documentary still, {shot}, grounded realistic details, "
        f"natural textures, cinematic lighting with shallow depth of field, "
        f"high dynamic range, 16:9 composition, no text, no logos. Scene: {hint}.{anchor_suffix}",
        f"Environmental portrait photography, {shot}, ambient natural light, "
        f"authentic documentary feel, rich color grading, editorial style. Scene: {hint}.{anchor_suffix}",
        f"Observational documentary frame, {shot}, available light photography, "
        f"authentic textures, environmental context, photojournalism aesthetic. Scene: {hint}.{anchor_suffix}",
    ]
    return random.choice(templates)


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
        return "vintage archival still with sepia tones"
    return "medium documentary shot"
    if any(w in lower for w in ("terminal", "screen", "бирж", "stock")):
        return "close-up of glowing screens"
    return "medium documentary shot"


def make_ltx_prompt(image_prompt: str, motion_hint: str = "") -> str:
    motion = motion_hint or _suggest_motion(image_prompt)
    return (
        f"The camera {motion}. "
        "Subtle natural movement brings the scene to life with realistic motion, "
        "stable composition, natural depth, no readable text, no logos. "
        "Gentle environmental movement where appropriate: "
        "light shifting, particles drifting, water rippling softly. "
        f"Visual intent: {image_prompt}"
    )


def _suggest_motion(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ("macro", "detail", "волокн")):
        return "holds steady on the macro detail, then slowly pulls back to reveal context"
    if any(w in lower for w in ("underwater", "океан", "под водой")):
        return "slowly drifts forward through the water, tracking the subject"
    if any(w in lower for w in ("ship", "судно", "кораб")):
        return "slowly orbits around the vessel, capturing its scale against the sea"
    if any(w in lower for w in ("data center", "сервер", "terminal")):
        return "glides smoothly between rows, revealing depth and scale"
    if any(w in lower for w in ("tower", "вышка", "antenna")):
        return "slowly tilts upward, revealing the full structure against the sky"
    if any(w in lower for w in ("cable", "кабель")):
        return "slowly pushes in along the cable, revealing internal fiber structure"
    return "slowly pushes in, revealing depth and fine detail"


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
    source = shot.description or shot.asset_query or shot.screen_text or "documentary technology visual"
    source = clean_visual_prompt_source(source)
    if shot.asset_query and shot.asset_query.lower() not in source.lower():
        source = f"{source}, {shot.asset_query}"
    shot_type = _suggest_shot_type(source)
    if shot.visual_type == "ai_typography_card":
        return (
            "Clean cinematic editorial title card, premium documentary style, strong composition, "
            "high contrast, 16:9 layout, minimal visual clutter. "
            f"Visible text, exactly: \"{shot.screen_text}\". Visual idea: {source}"
        )
    return (
        f"Photorealistic documentary still, {shot_type}, grounded realistic details, "
        "natural textures, cinematic lighting with shallow depth of field, "
        "high dynamic range, 16:9 composition, no text, no logos. "
        f"Scene: {source}"
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


def find_default_tool(name: str) -> str:
    from_path = shutil.which(name)
    if from_path:
        return from_path
    local_candidates = [
        Path.cwd() / "tools" / "whisper.cpp" / name,
        Path.cwd() / "tools" / "ffmpeg" / name,
        Path.cwd() / name,
    ]
    for candidate in local_candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def find_default_model() -> str:
    model_dirs = [
        Path.cwd() / "tools" / "whisper.cpp" / "models",
        Path.cwd() / "models",
    ]
    preferred = ("ggml-medium", "ggml-small", "ggml-base", "ggml-large", "ggml-turbo")
    for directory in model_dirs:
        if not directory.exists():
            continue
        models = sorted(directory.glob("*.bin"))
        for prefix in preferred:
            for model in models:
                if model.name.lower().startswith(prefix):
                    return str(model)
        if models:
            return str(models[0])
    return ""


def run_process(command: list[str], log_callback) -> int:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        log_callback(line.rstrip())
    return process.wait()


def transcribe_with_whisper_cpp(
    audio_path: Path,
    whisper_cli: Path,
    model_path: Path,
    ffmpeg_path: Path,
    language: str,
    output_dir: Path,
    log_callback,
) -> tuple[str, list[TranscriptSegment], Path | None, Path | None]:
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    if not whisper_cli.exists():
        raise FileNotFoundError(f"whisper-cli.exe not found: {whisper_cli}")
    if not model_path.exists():
        raise FileNotFoundError(f"Whisper model not found: {model_path}")
    if not ffmpeg_path.exists():
        raise FileNotFoundError(f"ffmpeg.exe not found: {ffmpeg_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="content-pipeline-") as temp_root:
        wav_path = Path(temp_root) / "voice_16khz.wav"
        log_callback("Converting audio to 16 kHz mono WAV")
        ffmpeg_command = [
            str(ffmpeg_path),
            "-y",
            "-i",
            str(audio_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ]
        code = run_process(ffmpeg_command, log_callback)
        if code != 0:
            raise RuntimeError("ffmpeg conversion failed")

        output_base = output_dir / "whisper_transcript"
        log_callback("Running whisper.cpp")
        whisper_command = [
            str(whisper_cli),
            "-m",
            str(model_path),
            "-f",
            str(wav_path),
            "-l",
            language.strip() or "ru",
            "-otxt",
            "-osrt",
            "-oj",
            "-of",
            str(output_base),
        ]
        code = run_process(whisper_command, log_callback)
        if code != 0:
            raise RuntimeError("whisper.cpp transcription failed")

    txt_path = output_base.with_suffix(".txt")
    json_path = output_base.with_suffix(".json")
    srt_path = output_base.with_suffix(".srt")
    transcript_text = txt_path.read_text(encoding="utf-8", errors="replace").strip() if txt_path.exists() else ""
    segments = parse_whisper_json(json_path) if json_path.exists() else []
    if not segments and srt_path.exists():
        segments = parse_srt_segments(srt_path)
    return transcript_text, segments, json_path if json_path.exists() else None, srt_path if srt_path.exists() else None


def parse_whisper_json(path: Path) -> list[TranscriptSegment]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    raw_segments = data.get("transcription") or data.get("segments") or []
    segments: list[TranscriptSegment] = []
    for item in raw_segments:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        timestamps = item.get("timestamps") or {}
        start = timestamps.get("from") or item.get("start") or item.get("t0") or "00:00:00"
        end = timestamps.get("to") or item.get("end") or item.get("t1") or start
        segments.append(TranscriptSegment(normalize_timestamp(start), normalize_timestamp(end), text))
    return segments


def parse_srt_segments(path: Path) -> list[TranscriptSegment]:
    content = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\s*\n", content.strip())
    segments: list[TranscriptSegment] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start, end = [normalize_timestamp(part.strip()) for part in lines[1].split("-->", 1)]
        text = " ".join(lines[2:]).strip()
        segments.append(TranscriptSegment(start, end, text))
    return segments


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
) -> list[Scene]:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    niche_prefix = {
        "documentary": "документального",
        "stories": "сторителлинг-канала",
        "crime": "криминального детектива",
        "educational": "образовательного",
    }.get(niche, "документального")
    style_block = (
        f"\nОБЩИЙ ВИЗУАЛЬНЫЙ СТИЛЬ ВСЕХ СЦЕН: {style_anchor}\n"
        if style_anchor else ""
    )
    character_block = (
        f"ПЕРСОНАЖ (должен быть одинаков во всех сценах): {character_description}\n"
        if character_description else ""
    )
    prompt = (
        f"Ты режиссёр монтажа {niche_prefix} YouTube-контента.\n\n"
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
) -> list[Scene]:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    compact_transcript = "\n".join(
        f"{index}|{segment.start}-{segment.end}|{segment.text.strip()}"
        for index, segment in enumerate(segments, start=1)
    )
    niche_prefix = {
        "documentary": "документального",
        "stories": "сторителлинг-канала",
        "crime": "криминального детектива",
        "educational": "образовательного",
    }.get(niche, "документального")
    style_block = (
        f"\nОБЩИЙ СТИЛЬ ВСЕХ СЦЕН: {style_anchor}\n"
        if style_anchor else ""
    )
    character_block = (
        f"ПЕРСОНАЖ (должен выглядеть одинаково во всех сценах): {character_description}\n"
        if character_description else ""
    )
    prompt = (
        f"Ты режиссёр монтажа {niche_prefix} YouTube-контента.\n\n"
        "У тебя есть точная транскрибация с таймкодами всего ролика. "
        "Сгруппируй соседние сегменты в смысловые сцены для монтажа.\n\n"
        "Для КАЖДОЙ сцены РАЗВЕРНИ текст в визуальную сцену:\n"
        "- Какую локацию, время суток, освещение мы видим?\n"
        "- Кто или что в кадре? (персонаж, объект, техника, природа)\n"
        "- Какое действие происходит?\n"
        "- С какого ракурса снят кадр? (общий план, средний, деталь, сверху, от первого лица)\n"
        f"{style_block}"
        f"{character_block}"
        "ВАЖНО: visual_direction должен быть КОНКРЕТНЫМ. Не просто 'Офис', а "
        "'Open-plan office, late afternoon, warm sunlight through venetian blinds, "
        "dust particles floating in the air, medium shot of a designer working at a wide monitor'.\n\n"
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
) -> list[Scene]:
    """Same as call_lm_studio_for_segments but streams scenes via on_scene callback
    as they arrive from LM Studio (server-sent events)."""
    endpoint = base_url.rstrip("/") + "/chat/completions"
    compact_transcript = "\n".join(
        f"{index}|{segment.start}-{segment.end}|{segment.text.strip()}"
        for index, segment in enumerate(segments, start=1)
    )
    niche_prefix = {
        "documentary": "документального",
        "stories": "сторителлинг-канала",
        "crime": "криминального детектива",
        "educational": "образовательного",
    }.get(niche, "документального")
    style_block = (
        f"\nОБЩИЙ СТИЛЬ ВСЕХ СЦЕН: {style_anchor}\n"
        if style_anchor else ""
    )
    character_block = (
        f"ПЕРСОНАЖ (должен выглядеть одинаково во всех сценах): {character_description}\n"
        if character_description else ""
    )
    prompt = (
        f"Ты режиссёр монтажа {niche_prefix} YouTube-контента.\n\n"
        "У тебя есть точная транскрибация с таймкодами всего ролика. "
        "Сгруппируй соседние сегменты в смысловые сцены для монтажа.\n\n"
        "Для КАЖДОЙ сцены РАЗВЕРНИ текст в визуальную сцену:\n"
        "- Какую локацию, время суток, освещение мы видим?\n"
        "- Кто или что в кадре? (персонаж, объект, техника, природа)\n"
        "- Какое действие происходит?\n"
        "- С какого ракурса снят кадр? (общий план, средний, деталь, сверху, от первого лица)\n"
        f"{style_block}"
        f"{character_block}"
        "ВАЖНО: visual_direction должен быть КОНКРЕТНЫМ. Не просто 'Офис', а "
        "'Open-plan office, late afternoon, warm sunlight through venetian blinds, "
        "dust particles floating in the air, medium shot of a designer working at a wide monitor'.\n\n"
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
) -> list[Shot]:
    endpoint = base_url.rstrip("/") + "/chat/completions"

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
                )
            )
        return parsed

    def _lm_chat(prompt: str) -> list[dict]:
        payload = {
            "model": model.strip() or "local-model",
            "messages": [
                {"role": "system", "content": "Отвечай только валидным JSON. Не добавляй markdown."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.35,
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

    def _generate_sub_prompt(shot: Shot, scene: Scene, segs: list, start_s: float, end_s: float) -> Shot:
        seg_text = "\n".join(f"  {s.start} - {s.end} | \"{s.text}\"" for s in segs)
        style_line = f"ОБЩИЙ СТИЛЬ: {style_anchor}\n" if style_anchor else ""
        char_line = f"ПЕРСОНАЖ: {character_description}\n" if character_description else ""
        prompt = (
            f"Ты visual director для YouTube-ролика.\n"
            f"Сцена: \"{scene.title}\"\n"
            f"Направление: {scene.visual_direction}\n"
            f"{style_line}{char_line}"
            f"Текст диктора на этом отрезке:\n{seg_text}\n\n"
            "Верни JSON-массив с одним объектом. Поля:\n"
            "- description (русский, для монтажёра, 1 предложение)\n"
            "- image_prompt (английский, детальный реалистичный промпт для Krea 2, 3-6 предложений, "
            "конкретные материалы/освещение/цвета, без абстракций)\n"
            "- video_prompt (английский, движение камеры/действие, present tense, 3-5 предложений)\n\n"
            "image_prompt правила:\n"
            "- Реалистический стиль, никаких абстракций\n"
            "- Конкретные материалы и текстуры (weathered metal, brushed aluminum, frosted glass)\n"
            "- Конкретное освещение (golden hour backlight, cold fluorescent overhead)\n"
            "- Варьируй первую фразу (Documentary editorial photograph of..., "
            "Cinematic establishing shot of..., Detailed macro view of...)\n"
            "- Без клише 'cinematic lighting' и 'photorealistic'\n"
            "- Избегай unreal, fantasy, alien\n\n"
            "video_prompt правила:\n"
            "- Фокус на движении/действии\n"
            "- Camera movements (slow dolly in, subtle tracking, gentle pan)\n"
            "- Audio ambient sounds\n"
            "- Не описывай внешний вид\n"
        )
        try:
            raw = _lm_chat(prompt)
            data = raw[0] if raw else {}
        except Exception:
            data = {}
        return Shot(
            index=shot.index,
            scene_index=shot.scene_index,
            start=seconds_to_timecode(start_s),
            end=seconds_to_timecode(end_s),
            visual_type=shot.visual_type,
            description=str(data.get("description", f"Visual for: {scene.title}")),
            asset_query=shot.asset_query,
            image_model=shot.image_model,
            video_model=shot.video_model,
            image_prompt=str(data.get("image_prompt", shot.image_prompt)),
            video_prompt=str(data.get("video_prompt", shot.video_prompt)),
            screen_text=shot.screen_text,
            motion=shot.motion,
            priority="medium",
            fallback=shot.fallback,
        )

    def _split_long_shot(shot: Shot, scene: Scene, scene_segs: list) -> list[Shot]:
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

        part_a = Shot(
            index=shot.index,
            scene_index=shot.scene_index,
            start=shot.start,
            end=seconds_to_timecode(split_at),
            visual_type=shot.visual_type,
            description=shot.description,
            asset_query=shot.asset_query,
            image_model=shot.image_model,
            video_model=shot.video_model,
            image_prompt=shot.image_prompt,
            video_prompt=shot.video_prompt,
            screen_text=shot.screen_text,
            motion=shot.motion,
            priority=shot.priority,
            fallback=shot.fallback,
        )

        part_b_segs = [
            s for s in shot_segs
            if timecode_to_seconds(s.start) >= split_at
        ]
        part_b = _generate_sub_prompt(shot, scene, part_b_segs, split_at, end_s)

        result = [part_a]
        result.extend(_split_long_shot(part_b, scene, scene_segs))
        return result

    INSTRUCTIONS = (
        "Каждый shot — AI image-to-video (Krea2 генерирует изображение, LTX 2.3 анимирует его).\n\n"
        "Верни только валидный JSON-массив. Каждый объект должен иметь поля: "
        "scene_index, start, end, visual_type, description, asset_query, image_model, video_model, "
        "image_prompt, video_prompt, screen_text, motion, priority, fallback.\n\n"
        "Для КАЖДОГО кадра image_model=krea2_turbo, video_model=ltx2.3_i2v.\n"
        "visual_type: ai_image_then_i2v (для всех стандартных кадров).\n\n"
        f"Настройка пользователя: {image_preference}.\n\n"
        "ПРАВИЛА ДЛЯ image_prompt (Krea 2 — текстовое изображение):\n"
        "- Пиши на английском, длинные детальные промпты (3-6 предложений), естественным языком, а не тегами.\n"
        "- Krea 2 понимает описательные предложения лучше, чем список ключевых слов через запятую.\n"
        "- Описывай: shot type, subject, texture/material, lighting, color palette, composition, atmosphere.\n"
        "- ВАЖНО: Чередуй shot types между соседними кадрами в рамках одной сцены. "
        "Если предыдущий кадр wide establishing — следующий делай close-up или macro detail. "
        "Никогда не ставь два одинаковых типа кадра подряд.\n"
        "- Konкретные материалы и текстуры: 'weathered metal', 'brushed aluminum', "
        "'frosted glass', 'rough concrete', 'polished marble', 'aged copper patina'.\n"
        "- Konкретное освещение: 'golden hour backlight', 'cold fluorescent overhead', "
        "'warm tungsten desk lamp', 'harsh noon sun casting hard shadows'.\n"
        "- Не начинай каждый промпт одинаково. Варьируй первую фразу: "
        "'Documentary editorial photograph of...', 'Cinematic establishing shot of...', "
        "'Detailed macro view of...', 'Wide environmental portrait of...'.\n"
        "- Избегай клише: 'cinematic lighting' и 'photorealistic' в каждом промпте — "
        "используй конкретные описания вместо общих слов.\n"
        "- Используй visual_direction из сцены как подсказку для визуального стиля.\n"
        "- Не добавляй текст на изображение (screen_text отдельным слоем).\n"
        "- Избегай в промптах: 'unreal', 'fantasy', 'alien' — всё должно выглядеть как реальная документальная съёмка.\n\n"
        "ПРИМЕР ХОРОШЕГО image_prompt (длинный, детальный, естественный язык):\n"
        "\"Extreme macro shot of polished fiber optic cable cross-section under laboratory lighting, "
        "brilliant blue and green light pulses visible through the transparent glass core, "
        "razor-sharp focus on the cable center with soft bokeh in the background, "
        "tiny dust particles floating in the air illuminated by the light, "
        "cold blue and teal color palette, scientific documentary aesthetic, "
        "shallow depth of field isolating the cable end.\"\n\n"
        "ПРИМЕР ПЛОХОГО image_prompt (короткий, теговый — НЕ ДЕЛАЙ ТАК):\n"
        "\"Fiber optic cable, close-up, blue light, documentary, cinematic.\"\n\n"
        "ПРАВИЛА ДЛЯ video_prompt (LTX 2.3):\n"
        "- Описывай ЧТО ПРОИСХОДИТ на кадре, а не КАК ОН ВЫГЛЯДИТ (image_prompt уже даёт визуал).\n"
        "- Фокус: motion/действие, camera movement, audio.\n"
        "- Используй present tense: 'The camera slowly pushes in', 'wind moves through the scene'.\n"
        "- Camera movements: 'slow dolly in', 'subtle tracking shot', 'gentle pan left', "
        "'handheld documentary feel', 'slow orbit around the subject'.\n"
        "- Audio: ambient sounds, music mood. Примеры: 'ocean waves in background', "
        "'wind howling softly', 'industrial machinery humming'.\n"
        "- 3-5 предложений в одном абзаце.\n"
        "- Не описывай внешний вид — модель видит картинку.\n\n"
        "ПРАВИЛА МАРШРУТИЗАЦИИ:\n"
        "- screen_text на русском, 2-7 слов.\n"
        "- Если в тексте сегмента есть числа, проценты, даты — вынеси в screen_text поверх AI-видео.\n"
        "- description на русском для монтажера.\n"
        "- image_prompt и video_prompt на английском.\n"
        "- Для схем/маршрутов/BGP/5G — motion_text или remotion_graphic.\n"
        "- Не выдумывай факты, работай только с текстом сегментов.\n"
        "- Таймкод кадра: start = первого сегмента в группе, end = последнего.\n"
    )

    if segments and hasattr(segments[0], "start") and hasattr(segments[0], "text"):
        all_shots: list[Shot] = []
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
            style_line = f"Общий стиль: {style_anchor}\n" if style_anchor else ""
            char_line = f"Персонаж: {character_description}\n" if character_description else ""
            prompt = (
                f"Ты visual director для YouTube-ролика.\n"
                f"Сцена {scene.index}: \"{scene.title}\"\n"
                f"Направление: {scene.visual_direction}\n"
                f"Надпись: {scene.screen_text}\n"
                f"{style_line}{char_line}"
                f"Сегменты этой сцены ({n_segs} шт.):\n{seg_lines}\n\n"
                "ШАГ 1 — РАЗВЕРНИ текст в полную визуальную сцену:\n"
                "  Из короткой фразы сделай конкретную сцену: "
                "какая локация, время суток, освещение, кто/что в кадре, "
                "какие материалы и текстуры, какая атмосфера.\n"
                "  Пример: 'он просыпается и берёт телефон' → "
                "спальня, утро, солнечный свет сквозь шторы, "
                "мужчина 30 лет в футболке тянется к телефону на тумбочке, "
                "мятое одеяло, деревянная мебель, тёплый свет.\n\n"
                "ШАГ 2 — создай 3-6 кадров с РАЗНЫМИ ракурсами:\n"
                "- Каждый кадр = уникальная визуальная идея\n"
                "- Чередуй shot types (общий → деталь → средний → неожиданный)\n"
                f"{' - Сохраняй консистентность: персонаж/объект/стиль одинаковы во всех кадрах\n' if style_anchor or character_description else ''}"
                "- Не повторяй один концепт дважды в одной сцене\n"
                "- Не пытайся покрыть каждый сегмент — покрывай каждую СМЫСЛОВУЮ ЕДИНИЦУ\n"
                "- Типично: 3-6 кадров на сцену, независимо от количества сегментов\n"
                "- Порядок кадров: от общего к частному или по логике повествования\n"
                "- Максимальная длительность одного кадра: 40 секунд (LTX limit)\n\n"
                f"{INSTRUCTIONS}"
            )
            try:
                raw = _lm_chat(prompt)
                scene_shots = _parse_shots(raw, offset=len(all_shots))
                for s in scene_shots:
                    s.scene_index = scene.index
                split_shots = []
                for s in scene_shots:
                    split_shots.extend(_split_long_shot(s, scene, scene_segs))
                for i, s in enumerate(split_shots):
                    s.index = len(all_shots) + i + 1
                all_shots.extend(split_shots)
                if on_scene_shots:
                    on_scene_shots(split_shots)
            except Exception as exc:
                raise RuntimeError(
                    f"LM Studio ошибка на сцене {scene.index} \"{scene.title}\": {exc}"
                ) from exc
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
    normalize_shot_routes(shots, image_preference=image_preference)
    repair_shot_prompts(shots)
    return shots



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
    typography_by_scene: dict[int, int] = {}
    preferred_model = preferred_realistic_image_model(image_preference)
    preference = image_preference.strip().lower()
    for shot in shots:
        visual_type = shot.visual_type.lower()
        text = f"{shot.description} {shot.screen_text} {shot.asset_query}".lower()

        if visual_type == "ai_typography_card":
            count = typography_by_scene.get(shot.scene_index, 0)
            if count >= 1:
                shot.visual_type = "motion_text" if shot.screen_text else "ai_image_then_i2v"
                shot.image_model = ""
                shot.video_model = ""
                shot.fallback = shot.fallback or "Remotion text overlay or AI image-to-video"
            else:
                typography_by_scene[shot.scene_index] = count + 1

        if visual_type in {"programmatic_schema", "remotion_graphic", "map_graphic", "motion_text"}:
            shot.image_model = ""
            shot.video_model = ""
            if not shot.fallback:
                shot.fallback = "Remotion/SVG generated graphic"

        if any(keyword in text for keyword in ("bgp", "маршрут", "обход", "chokepoint", "узк", "карта", "map")):
            if shot.visual_type in {"ai_image", "ai_image_then_i2v", "ai_typography_card"}:
                shot.visual_type = "remotion_graphic"
                shot.image_model = ""
                shot.video_model = ""
                shot.fallback = "Remotion map or route animation"

        if shot.screen_text and shot.visual_type == "ai_image" and not screen_text_has_number(shot.screen_text):
            shot.visual_type = "motion_text"
            shot.image_model = ""
            shot.video_model = ""
            shot.fallback = "Text overlay in Remotion"

        if shot.visual_type in {"local_broll", "stock_broll", "local_broll_or_ai_image"}:
            shot.visual_type = "ai_image_then_i2v"

        if shot.visual_type in {"ai_image", "ai_image_then_i2v"}:
            if shot.image_model != "ideogram4" or preference in {"krea", "ideogram"}:
                shot.image_model = preferred_model
            shot.video_model = shot.video_model or "ltx2.3_i2v"
            if shot.visual_type == "ai_image":
                shot.visual_type = "ai_image_then_i2v"

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
    renders_dir = project_dir / "renders" / "remotion"
    seen_remotion: set[tuple[str, str]] = set()

    cards: list[AssetCard] = []
    for shot in shots:
        old = existing_by_shot.get(shot.index)
        remotion_job = remotion_job_for_shot(shot, renders_dir)
        if remotion_job:
            dedupe_key = remotion_dedupe_key(remotion_job)
            if dedupe_key in seen_remotion:
                remotion_job = None
            else:
                seen_remotion.add(dedupe_key)
        remotion_template = remotion_job["props"]["template"] if remotion_job else ""
        remotion_output = remotion_job["output"] if remotion_job else ""
        route = determine_asset_route(shot, remotion_template)
        if route == "graphic":
            image_model = ""
            video_model = ""
        else:
            image_model = old.image_model if old and old.image_model else shot.image_model
            video_model = old.video_model if old and old.video_model else shot.video_model
        image_output = old.image_output if old else find_existing_asset(project_dir, "images", shot.index)
        video_output = old.video_output if old else find_existing_asset(project_dir, "videos", shot.index)
        if remotion_output and Path(remotion_output).exists():
            remotion_output = str(Path(remotion_output))

        cards.append(
            AssetCard(
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
                remotion_template=remotion_template,
                image_prompt=old.image_prompt if old and old.image_prompt else shot.image_prompt,
                video_prompt=old.video_prompt if old and old.video_prompt else shot.video_prompt,
                screen_text=old.screen_text if old and old.screen_text else shot.screen_text,
                motion=shot.motion,
                fallback=shot.fallback,
                image_output=image_output,
                video_output=video_output,
                remotion_output=old.remotion_output if old and old.remotion_output else remotion_output,
                status=old.status if old else "planned",
                notes=old.notes if old else "",
                approved=old.approved if old else False,
            )
        )
    return cards


def determine_asset_route(shot: Shot, remotion_template: str) -> str:
    if remotion_template:
        return "remotion"
    if shot.visual_type in {"motion_text", "programmatic_schema", "map_graphic", "remotion_graphic"}:
        return "graphic"
    return "image+video"

def load_asset_board(project_dir: Path) -> list[AssetCard]:
    path = project_dir / "06_asset_board.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    cards: list[AssetCard] = []
    field_defaults = {
        "image_output": "",
        "video_output": "",
        "remotion_output": "",
        "status": "planned",
        "notes": "",
        "approved": False,
        "timeline_start": "",
        "timeline_end": "",
        "assigned_media": "",
        "track": "video_1",
    }
    for item in raw:
        data = {**field_defaults, **item}
        cards.append(AssetCard(**data))
    return cards


def write_asset_board(project_dir: Path, cards: list[AssetCard]):
    (project_dir / "06_asset_board.json").write_text(
        json.dumps([card.__dict__ for card in cards], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def find_existing_asset(project_dir: Path, folder: str, shot_index: int) -> str:
    base = project_dir / "assets" / folder
    if not base.exists():
        return ""
    matches = sorted(base.glob(f"shot_{shot_index:03d}*"))
    return str(matches[0]) if matches else ""


def comfy_base_url(url: str) -> str:
    value = url.strip().rstrip("/")
    return value or "http://127.0.0.1:8188"


def comfy_request_json(url: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def comfy_queue_prompt(base_url: str, workflow: dict) -> str:
    payload = {"prompt": workflow, "client_id": str(uuid.uuid4())}
    result = comfy_request_json(f"{comfy_base_url(base_url)}/prompt", payload, timeout=60)
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return prompt_id: {result}")
    return prompt_id


def comfy_wait_for_history(base_url: str, prompt_id: str, timeout_seconds: int = 1800) -> dict:
    deadline = time.time() + timeout_seconds
    history_url = f"{comfy_base_url(base_url)}/history/{urllib.parse.quote(prompt_id)}"
    last_error = ""
    while time.time() < deadline:
        try:
            result = comfy_request_json(history_url, timeout=20)
            item = result.get(prompt_id)
            if item:
                status = item.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(json.dumps(status, ensure_ascii=False))
                if item.get("outputs"):
                    return item
        except urllib.error.URLError as exc:
            last_error = str(exc)
        time.sleep(2)
    raise TimeoutError(f"ComfyUI generation timeout. Last error: {last_error}")


def comfy_collect_outputs(history_item: dict, kinds: tuple[str, ...]) -> list[dict]:
    found: list[dict] = []
    for node_output in history_item.get("outputs", {}).values():
        for key in kinds:
            for item in node_output.get(key, []):
                found.append(item)
    return found


def comfy_download_output(base_url: str, item: dict, target_dir: Path, shot_index: int, prefix: str) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = item.get("filename", "")
    if not filename:
        raise RuntimeError(f"ComfyUI output has no filename: {item}")
    query = urllib.parse.urlencode(
        {
            "filename": filename,
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        }
    )
    url = f"{comfy_base_url(base_url)}/view?{query}"
    ext = Path(filename).suffix or ".bin"
    target = target_dir / f"shot_{shot_index:03d}_{prefix}{ext}"
    with urllib.request.urlopen(url, timeout=120) as response:
        target.write_bytes(response.read())
    return target


def comfy_upload_image(base_url: str, image_path: Path, name: str) -> str:
    boundary = "----ContentPipelineBoundary" + uuid.uuid4().hex
    file_bytes = image_path.read_bytes()
    safe_name = f"{name}{image_path.suffix.lower() or '.png'}"
    parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{safe_name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
        + file_bytes
        + b"\r\n",
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'.encode("utf-8"),
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(part if isinstance(part, bytes) else part.encode("utf-8") for part in parts)
    request = urllib.request.Request(
        f"{comfy_base_url(base_url)}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.loads(response.read().decode("utf-8"))
    filename = result.get("name", safe_name)
    subfolder = result.get("subfolder", "")
    return f"{subfolder}/{filename}" if subfolder else filename


def set_workflow_seed_values(workflow: dict):
    seed = int(time.time() * 1000) % 900000000000000
    for node in workflow.values():
        inputs = node.get("inputs", {})
        for key in list(inputs.keys()):
            if key in {"seed", "noise_seed", "sampling_mode.seed"}:
                inputs[key] = seed
                seed += 1


def configured_workflow_path(setting_key: str, default_path: Path) -> Path:
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            configured = str(settings.get(setting_key, "")).strip()
            if configured:
                return Path(configured)
        except (json.JSONDecodeError, OSError):
            pass
    return default_path


def prepare_image_workflow(model: str, prompt: str, shot_index: int) -> dict:
    if model == "ideogram4":
        workflow_path = configured_workflow_path("comfy_ideogram_workflow", DEFAULT_COMFY_IDEOGRAM_WORKFLOW)
    else:
        workflow_path = configured_workflow_path("comfy_krea_workflow", DEFAULT_COMFY_KREA_WORKFLOW)
        model = "krea2_turbo"
    if not workflow_path.exists():
        raise FileNotFoundError(f"Workflow not found: {workflow_path}")
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    if model == "ideogram4":
        workflow["98:24"]["inputs"]["text"] = prompt
        workflow["158"]["inputs"]["filename_prefix"] = f"content_pipeline/shot_{shot_index:03d}_ideogram4"
    else:
        workflow["30:19"]["inputs"]["value"] = prompt
        workflow["29"]["inputs"]["filename_prefix"] = f"content_pipeline/shot_{shot_index:03d}_krea2"
    set_workflow_seed_values(workflow)
    return workflow


def prepare_ltx_workflow(prompt: str, input_image_name: str, shot_index: int, duration_seconds: float = 10.0) -> dict:
    workflow_path = configured_workflow_path("comfy_ltx_workflow", DEFAULT_COMFY_LTX_WORKFLOW)
    if not workflow_path.exists():
        raise FileNotFoundError(f"Workflow not found: {workflow_path}")
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow["269"]["inputs"]["image"] = input_image_name
    workflow["267:266"]["inputs"]["value"] = prompt
    workflow["273"]["inputs"]["filename_prefix"] = f"content_pipeline/video/shot_{shot_index:03d}_ltx2_3"
    duration = max(2.0, min(duration_seconds, MAX_LTX_DURATION))
    workflow["267:290"]["inputs"]["value"] = duration
    set_workflow_seed_values(workflow)
    return workflow


def generate_comfy_image(base_url: str, project_dir: Path, card: AssetCard) -> Path:
    model = card.image_model or "krea2_turbo"
    prompt = card.image_prompt.strip()
    if not prompt:
        raise ValueError("Image prompt is empty")
    workflow = prepare_image_workflow(model, prompt, card.shot_index)
    prompt_id = comfy_queue_prompt(base_url, workflow)
    history = comfy_wait_for_history(base_url, prompt_id)
    outputs = comfy_collect_outputs(history, ("images",))
    if not outputs:
        raise RuntimeError("ComfyUI finished, but no image output was found")
    prefix = model.replace(".", "_").replace("-", "_")
    return comfy_download_output(base_url, outputs[-1], project_dir / "assets" / "images", card.shot_index, prefix)


def generate_comfy_ltx_video(base_url: str, project_dir: Path, card: AssetCard) -> Path:
    if not card.image_output or not Path(card.image_output).exists():
        raise FileNotFoundError("Для LTX сначала нужна готовая картинка в image_output.")
    prompt = card.video_prompt.strip() or card.image_prompt.strip()
    if not prompt:
        raise ValueError("Video prompt is empty")
    duration = max(2.0, timecode_to_seconds(card.end) - timecode_to_seconds(card.start))
    duration = min(duration, MAX_LTX_DURATION)
    upload_name = comfy_upload_image(base_url, Path(card.image_output), f"shot_{card.shot_index:03d}_ltx_input")
    workflow = prepare_ltx_workflow(prompt, upload_name, card.shot_index, duration_seconds=duration)
    prompt_id = comfy_queue_prompt(base_url, workflow)
    history = comfy_wait_for_history(base_url, prompt_id, timeout_seconds=3600)
    outputs = comfy_collect_outputs(history, ("videos", "gifs", "images"))
    if not outputs:
        raise RuntimeError("ComfyUI finished, but no video output was found")
    return comfy_download_output(base_url, outputs[-1], project_dir / "assets" / "videos", card.shot_index, "ltx2_3")



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

def remotion_job_for_shot(shot: Shot, renders_dir: Path) -> dict | None:
    duration = max(2, timecode_to_seconds(shot.end) - timecode_to_seconds(shot.start))
    visual_type = shot.visual_type.lower()
    text = f"{shot.description} {shot.screen_text} {shot.asset_query}".lower()
    if visual_type in {"programmatic_schema", "remotion_graphic", "motion_text", "map_graphic"}:
        if any(word in text for word in ("hair", "human hair", "anatomy", "exploded", "cutaway", "cross-section", "owners", "logos")):
            return None
        if "bgp" in text or "обход" in text or "reroute" in text or "routing" in text or "маршрут" in text and "повреж" in text:
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
            "6. Remotion",
            "7. Настройки",
            "8. Экспорт (позже)",
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
        self.build_remotion_section()
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
        ttk.Combobox(row1, textvariable=self.asset_route, values=["remotion", "image+video", "image", "broll_or_ai", "graphic", "manual"], state="readonly", width=14).pack(side=LEFT, padx=(6, 14))
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
        Button(generate_actions, text="Generate route", command=self.start_generate_selected_route).pack(side=LEFT)
        Button(generate_actions, text="Generate graphic", command=self.start_generate_selected_graphic).pack(side=LEFT, padx=(8, 0))
        Button(generate_actions, text="Generate image", command=self.start_generate_selected_image).pack(side=LEFT, padx=(8, 0))
        Button(generate_actions, text="Generate LTX video", command=self.start_generate_selected_video).pack(side=LEFT, padx=(8, 0))
        Button(generate_actions, text="Render Remotion", command=self.start_render_selected_remotion).pack(side=LEFT, padx=(8, 0))

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
        self.section_frames[6] = frame

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
        self.section_frames[7] = frame
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
        if card.remotion_output:
            outputs.append(f"Remotion: {card.remotion_output}")
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
        for value in [card.video_output, card.remotion_output, card.image_output]:
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

            if mode == "route":
                if card.route == "remotion":
                    self.render_asset_remotion(card, cards, card_index)
                    return
                if card.route == "graphic" and card.visual_type == "motion_text":
                    self.render_asset_motion_text(card, cards, card_index)
                    return
                if card.route in {"image+video", "image", "broll_or_ai", "graphic"}:
                    generated_any = False
                    if card.image_model:
                        image_path = generate_comfy_image(self.comfy_url.get(), project_dir, card)
                        card.image_output = str(image_path)
                        generated_any = True
                    if card.route == "image+video" and card.video_model:
                        video_path = generate_comfy_ltx_video(self.comfy_url.get(), project_dir, card)
                        video_path = apply_number_overlay_to_video(video_path, project_dir, card, self.log_queue.put)
                        card.video_output = str(video_path)
                        generated_any = True
                    if not generated_any:
                        raise RuntimeError(f"Route '{card.route}' needs manual setup or a selected image model.")
                else:
                    raise RuntimeError(f"Route '{card.route}' пока не генерируется автоматически.")
            elif mode == "image":
                image_path = generate_comfy_image(self.comfy_url.get(), project_dir, card)
                card.image_output = str(image_path)
            elif mode == "video":
                video_path = generate_comfy_ltx_video(self.comfy_url.get(), project_dir, card)
                video_path = apply_number_overlay_to_video(video_path, project_dir, card, self.log_queue.put)
                card.video_output = str(video_path)
            elif mode == "graphic":
                self.render_asset_motion_text(card, cards, card_index)
                return
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
                    shots = call_lm_studio_for_shots(
                        self.lm_url.get(),
                        self.lm_model.get(),
                        scenes,
                        image_preference=self.image_model_preference.get(),
                        ttl_seconds=self.lm_ttl_seconds(),
                        segments=tm_segments,
                    )
                    self.log_queue.put(f"Visual stage 2/3: LM Studio returned {len(shots)} frame(s)")
                    self.schedule_lm_unload("frame list generation finished")
                except Exception as exc:
                    self.log_queue.put(f"LM Studio visual plan failed: {explain_lm_studio_error(exc)}")
                    self.log_queue.put("Falling back to local frame-list draft")
                    shots = build_heuristic_shots(scenes, image_preference=self.image_model_preference.get())
            else:
                self.log_queue.put("Visual stage 1/3: building local frame-list draft")
                shots = build_heuristic_shots(scenes, image_preference=self.image_model_preference.get())
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


