import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from core.models import AssetCard, timecode_to_seconds

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_SIZE = "1920:1080"
FPS = "25"


def find_voice_audio(project_dir: Path) -> Path | None:
    voice_dir = project_dir / "voice"
    if not voice_dir.exists():
        return None
    audio_files = sorted(
        path for path in voice_dir.iterdir()
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    )
    return audio_files[0] if audio_files else None


def timeline_voice_start_seconds(project_dir: Path) -> int:
    path = project_dir / "08_timeline.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return max(0, timecode_to_seconds(data.get("voice_start", "00:00:00")))
    except (json.JSONDecodeError, OSError, ValueError):
        return 0


def card_duration(card: AssetCard) -> int:
    start_s = timecode_to_seconds(card.timeline_start or card.start)
    end_s = timecode_to_seconds(card.timeline_end or card.end)
    return max(2, end_s - start_s)


def card_source(card: AssetCard) -> Path | None:
    for value in (card.assigned_media, card.video_output, card.image_output):
        if value and Path(value).exists():
            return Path(value)
    return None


# Single source of truth for "is this card part of the final video".
# A card is included when it has usable media AND was not explicitly excluded.
# This replaces the old `approved` whitelist so the timeline preview, the
# Timeline preview and this ffmpeg export agree on what ships.
EXCLUDED_STATUSES = {"skipped", "rejected"}


def card_included(card: AssetCard) -> bool:
    if (getattr(card, "status", "") or "").lower() in EXCLUDED_STATUSES:
        return False
    return card_source(card) is not None


def card_overlay_text(card: AssetCard) -> str:
    if not getattr(card, "text_overlay_enabled", False):
        return ""
    return (getattr(card, "text_overlay_text", "") or card.screen_text or "").strip()


def ffmpeg_filter_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def find_overlay_font() -> Path | None:
    for font_path in (
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\tahoma.ttf"),
    ):
        if font_path.exists():
            return font_path
    return None


def _clamp_float(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _overlay_style(card: AssetCard) -> dict:
    style = (getattr(card, "text_overlay_style", "subtitle") or "subtitle").lower()
    styles = {
        "subtitle": {"fontsize": 54, "borderw": 3, "shadowx": 2, "shadowy": 2, "default_y": 86, "x": "(w-text_w)/2"},
        "title": {"fontsize": 86, "borderw": 4, "shadowx": 3, "shadowy": 3, "default_y": 48, "x": "(w-text_w)/2"},
        "caption": {"fontsize": 44, "borderw": 3, "shadowx": 2, "shadowy": 2, "default_y": 78, "x": "w*0.07"},
        "label": {"fontsize": 50, "borderw": 3, "shadowx": 2, "shadowy": 2, "default_y": 18, "x": "w*0.07"},
        "quote": {"fontsize": 66, "borderw": 4, "shadowx": 3, "shadowy": 3, "default_y": 50, "x": "(w-text_w)/2"},
    }
    return styles.get(style, styles["subtitle"])


def _overlay_xy(card: AssetCard, style_cfg: dict) -> tuple[str, str]:
    position = (getattr(card, "text_overlay_position", "bottom") or "bottom").lower()
    if position == "manual":
        x = _clamp_float(getattr(card, "text_overlay_x", 50), 50, 0, 100)
        y = _clamp_float(getattr(card, "text_overlay_y", style_cfg["default_y"]), style_cfg["default_y"], 0, 100)
        return f"(w-text_w)*{x / 100:.4f}", f"(h-text_h)*{y / 100:.4f}"
    if position == "top":
        return style_cfg["x"], "h*0.12"
    if position == "center":
        return "(w-text_w)/2", "(h-text_h)/2"
    return style_cfg["x"], "h*0.82"


def _overlay_alpha(card: AssetCard) -> str:
    enter = (getattr(card, "text_overlay_enter", "fade") or "none").lower()
    exit_ = (getattr(card, "text_overlay_exit", "fade") or "none").lower()
    duration = max(0.2, float(card_duration(card)))
    fade = min(0.35, duration / 3)
    fade_in = fade if enter == "fade" else 0.0
    fade_out = fade if exit_ == "fade" else 0.0
    if fade_in and fade_out:
        return f"if(lt(t,{fade_in:.3f}),t/{fade_in:.3f},if(gt(t,{duration - fade_out:.3f}),({duration:.3f}-t)/{fade_out:.3f},1))"
    if fade_in:
        return f"if(lt(t,{fade_in:.3f}),t/{fade_in:.3f},1)"
    if fade_out:
        return f"if(gt(t,{duration - fade_out:.3f}),({duration:.3f}-t)/{fade_out:.3f},1)"
    return "1"


def overlay_filter(card: AssetCard, dst: Path) -> str:
    text = card_overlay_text(card)
    if not text:
        return ""

    text_path = dst.with_suffix(".overlay.txt")
    text_path.write_text(text, encoding="utf-8")

    style_cfg = _overlay_style(card)
    x_expr, y_expr = _overlay_xy(card, style_cfg)
    font = find_overlay_font()
    font_part = f"fontfile='{ffmpeg_filter_path(font)}':" if font else ""
    text_file = ffmpeg_filter_path(text_path)
    alpha = _overlay_alpha(card).replace(",", r"\,")
    return (
        f"drawtext={font_part}textfile='{text_file}':fontcolor=white:"
        f"fontsize={style_cfg['fontsize']}:borderw={style_cfg['borderw']}:bordercolor=black@0.72:"
        f"shadowx={style_cfg['shadowx']}:shadowy={style_cfg['shadowy']}:shadowcolor=black@0.55:"
        f"x={x_expr}:y={y_expr}:alpha='{alpha}'"
    )


def has_text_overlays(cards: list[AssetCard]) -> bool:
    return any(card_overlay_text(card) for card in cards if card_included(card))


def run_ffmpeg(cmd: list[str], log_callback: Callable | None = None) -> bool:
    if log_callback:
        log_callback(f"Running: {' '.join(cmd)}")
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            if log_callback:
                log_callback(line.rstrip())
        code = process.wait()
        if code != 0:
            if log_callback:
                log_callback(f"FFmpeg exited with code {code}")
            return False
        return True
    except FileNotFoundError:
        if log_callback:
            log_callback(f"ffmpeg not found: {cmd[0]}")
        return False
    except Exception as exc:
        if log_callback:
            log_callback(f"Export error: {exc}")
        return False


def build_concat_file_from_paths(paths: list[Path], output_path: Path) -> str:
    if not paths:
        return ""
    output_path.mkdir(parents=True, exist_ok=True)
    lines = [f"file '{str(path.resolve())}'" for path in paths]
    concat_path = output_path / "_concat_list.txt"
    concat_path.write_text("\n".join(lines), encoding="utf-8")
    return str(concat_path)


def build_concat_file(cards: list[AssetCard], output_path: Path) -> str:
    """Build a legacy FFmpeg concat file from included generated outputs."""
    lines = []
    for card in cards:
        if not card_included(card):
            continue
        src = card_source(card)
        if src:
            lines.append(f"file '{str(src.resolve())}'")
            lines.append(f"duration {card_duration(card)}")
    if not lines:
        return ""
    output_path.mkdir(parents=True, exist_ok=True)
    concat_path = output_path / "_concat_list.txt"
    concat_path.write_text("\n".join(lines), encoding="utf-8")
    return str(concat_path)


def normalize_segment(
    ffmpeg: str,
    src: Path,
    dst: Path,
    duration: int,
    card: AssetCard,
    log_callback: Callable | None = None,
) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    filters = [
        f"fps={FPS}",
        f"scale={VIDEO_SIZE}:force_original_aspect_ratio=decrease",
        f"pad={VIDEO_SIZE}:(ow-iw)/2:(oh-ih)/2",
        f"tpad=stop_mode=clone:stop_duration={duration}",
    ]
    overlay = overlay_filter(card, dst)
    if overlay:
        if log_callback:
            log_callback(f"Applying text overlay to shot {card.shot_index:03d}: {card_overlay_text(card)}")
        filters.append(overlay)
    vf = ",".join(filters)
    if src.suffix.lower() in IMAGE_EXTENSIONS:
        cmd = [
            ffmpeg, "-y", "-loop", "1", "-i", str(src),
            "-t", str(duration), "-vf", vf,
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", FPS, "-movflags", "+faststart",
            str(dst),
        ]
    else:
        cmd = [
            ffmpeg, "-y", "-i", str(src),
            "-t", str(duration), "-vf", vf,
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", FPS, "-movflags", "+faststart",
            str(dst),
        ]
    return run_ffmpeg(cmd, log_callback)


def build_normalized_concat_file(
    cards: list[AssetCard],
    exports_dir: Path,
    ffmpeg: str,
    log_callback: Callable | None = None,
) -> str:
    segments_dir = exports_dir / "_normalized_segments"
    segment_paths: list[Path] = []
    included = sorted(
        [card for card in cards if card_included(card)],
        key=lambda card: timecode_to_seconds(card.timeline_start or card.start),
    )
    for index, card in enumerate(included, start=1):
        src = card_source(card)
        if not src:
            continue
        duration = card_duration(card)
        dst = segments_dir / f"segment_{index:03d}_shot_{card.shot_index:03d}.mp4"
        if log_callback:
            log_callback(f"Normalizing shot {card.shot_index:03d} to {duration}s")
        if not normalize_segment(ffmpeg, src, dst, duration, card, log_callback):
            return ""
        segment_paths.append(dst)
    return build_concat_file_from_paths(segment_paths, exports_dir)


def export_video(
    project_dir: Path,
    ffmpeg_path: str = "ffmpeg",
    log_callback: Callable | None = None,
    output_name: str = "final_cut.mp4",
) -> Path | None:
    """Export the final video by concatenating included card outputs (media present, not excluded)."""
    board_path = project_dir / "06_asset_board.json"
    if not board_path.exists():
        board_path = project_dir / "asset_board.json"
    if not board_path.exists():
        if log_callback:
            log_callback("Asset board not found.")
        return None

    try:
        board_data = json.loads(board_path.read_text(encoding="utf-8"))
        legacy_defaults = {"remotion_template": "", "remotion_output": ""}
        cards = [AssetCard(**{**legacy_defaults, **item}) for item in board_data]
    except (json.JSONDecodeError, OSError) as exc:
        if log_callback:
            log_callback(f"Failed to read asset board: {exc}")
        return None

    included = sorted(
        [c for c in cards if card_included(c)],
        key=lambda card: timecode_to_seconds(card.timeline_start or card.start),
    )
    if not included:
        if log_callback:
            log_callback("No cards to export (need generated media that isn't excluded).")
        return None

    exports_dir = project_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    output_path = exports_dir / output_name

    ffmpeg = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg") or ffmpeg_path
    voice_audio = find_voice_audio(project_dir)
    voice_start = timeline_voice_start_seconds(project_dir)
    overlays_enabled = has_text_overlays(cards)
    if voice_audio:
        if log_callback:
            log_callback(f"Using voice audio: {voice_audio}")
            if voice_start:
                log_callback(f"Voice timeline offset: {voice_start}s")
        concat_file = build_normalized_concat_file(cards, exports_dir, ffmpeg, log_callback)
    elif overlays_enabled:
        if log_callback:
            log_callback("No voice audio found; exporting normalized video with timeline text overlays.")
        concat_file = build_normalized_concat_file(cards, exports_dir, ffmpeg, log_callback)
    else:
        if log_callback:
            log_callback("No voice audio found in project voice folder; exporting generated clip audio only.")
        concat_file = build_concat_file(cards, exports_dir)

    if not concat_file:
        if log_callback:
            log_callback("No generated outputs found for included cards.")
        return None

    if voice_audio:
        cmd = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-i", str(voice_audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        ]
        if voice_start:
            delay_ms = voice_start * 1000
            cmd.extend(["-af", f"adelay={delay_ms}:all=1"])
        cmd.extend([
            "-shortest",
            str(output_path),
        ])
    else:
        cmd = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-c", "copy",
            str(output_path),
        ]

    if not run_ffmpeg(cmd, log_callback):
        return None

    if log_callback:
        log_callback(f"Export complete: {output_path}")
    return output_path


