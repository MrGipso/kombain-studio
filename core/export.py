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
    for value in (card.assigned_media, card.video_output, card.remotion_output, card.image_output):
        if value and Path(value).exists():
            return Path(value)
    return None


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
    """Build a legacy FFmpeg concat file from approved generated outputs."""
    lines = []
    for card in cards:
        if not card.approved:
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
    log_callback: Callable | None = None,
) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"fps={FPS},"
        f"scale={VIDEO_SIZE}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_SIZE}:(ow-iw)/2:(oh-ih)/2,"
        f"tpad=stop_mode=clone:stop_duration={duration}"
    )
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
    approved = sorted(
        [card for card in cards if card.approved],
        key=lambda card: timecode_to_seconds(card.timeline_start or card.start),
    )
    for index, card in enumerate(approved, start=1):
        src = card_source(card)
        if not src:
            continue
        duration = card_duration(card)
        dst = segments_dir / f"segment_{index:03d}_shot_{card.shot_index:03d}.mp4"
        if log_callback:
            log_callback(f"Normalizing shot {card.shot_index:03d} to {duration}s")
        if not normalize_segment(ffmpeg, src, dst, duration, log_callback):
            return ""
        segment_paths.append(dst)
    return build_concat_file_from_paths(segment_paths, exports_dir)


def export_video(
    project_dir: Path,
    ffmpeg_path: str = "ffmpeg",
    log_callback: Callable | None = None,
    output_name: str = "final_cut.mp4",
) -> Path | None:
    """Export the final video by concatenating approved card outputs."""
    board_path = project_dir / "06_asset_board.json"
    if not board_path.exists():
        board_path = project_dir / "asset_board.json"
    if not board_path.exists():
        if log_callback:
            log_callback("Asset board not found.")
        return None

    try:
        board_data = json.loads(board_path.read_text(encoding="utf-8"))
        cards = [AssetCard(**item) for item in board_data]
    except (json.JSONDecodeError, OSError) as exc:
        if log_callback:
            log_callback(f"Failed to read asset board: {exc}")
        return None

    approved = sorted(
        [c for c in cards if c.approved],
        key=lambda card: timecode_to_seconds(card.timeline_start or card.start),
    )
    if not approved:
        if log_callback:
            log_callback("No approved cards to export.")
        return None

    exports_dir = project_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    output_path = exports_dir / output_name

    ffmpeg = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg") or ffmpeg_path
    voice_audio = find_voice_audio(project_dir)
    voice_start = timeline_voice_start_seconds(project_dir)
    if voice_audio:
        if log_callback:
            log_callback(f"Using voice audio: {voice_audio}")
            if voice_start:
                log_callback(f"Voice timeline offset: {voice_start}s")
        concat_file = build_normalized_concat_file(cards, exports_dir, ffmpeg, log_callback)
    else:
        if log_callback:
            log_callback("No voice audio found in project voice folder; exporting generated clip audio only.")
        concat_file = build_concat_file(cards, exports_dir)

    if not concat_file:
        if log_callback:
            log_callback("No generated outputs found for approved cards.")
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
