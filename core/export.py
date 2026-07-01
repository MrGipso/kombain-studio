import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from core.models import AssetCard, timecode_to_seconds

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_SIZE = "1920:1080"
FPS = "25"


def tiled_intervals(starts_seconds: list[int], total_end: int, min_dur: int = 1) -> list[tuple[int, int]]:
    """Turn a list of clip START times into a CONTIGUOUS set of [start, end)
    intervals that tile the whole timeline [0, total_end] with no gaps and no
    overlaps. Each clip is shown until the next clip starts; the first clip covers
    the lead-in from 0; the last clip runs to total_end. This is the single source
    of truth for 'a clip occupies the time until the next one' — used both by the
    exporter and by the shot re-tiler, so the timeline, board and render agree."""
    n = len(starts_seconds)
    if n == 0:
        return []
    # First boundary at 0 so there is never a black lead-in; subsequent boundaries
    # are the clip starts, forced strictly increasing to absorb overlaps.
    bounds = [0]
    prev = 0
    for i in range(1, n):
        st = int(starts_seconds[i])
        if st < prev + min_dur:
            st = prev + min_dur
        bounds.append(st)
        prev = st
    end = int(total_end)
    if end < prev + min_dur:
        end = prev + min_dur
    bounds.append(end)
    return [(bounds[i], bounds[i + 1]) for i in range(n)]


def _ffprobe_bin(ffmpeg: str) -> str:
    found = shutil.which("ffprobe.exe") or shutil.which("ffprobe")
    if found:
        return found
    ffmpeg_path = Path(ffmpeg)
    for name in ("ffprobe.exe", "ffprobe"):
        candidate = ffmpeg_path.with_name(name)
        if candidate.exists():
            return str(candidate)
    return "ffprobe"


def media_duration_seconds(path: Path, ffmpeg: str = "ffmpeg") -> float:
    """Duration of a media file in seconds (0.0 if it can't be probed)."""
    if not path or not Path(path).exists():
        return 0.0
    try:
        out = subprocess.check_output(
            [_ffprobe_bin(ffmpeg), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            text=True, timeout=30,
        ).strip()
        return float(out)
    except Exception:
        return 0.0


_NVENC_CACHE: bool | None = None


def nvenc_available(ffmpeg: str) -> bool:
    """True if this ffmpeg build exposes the NVIDIA h264_nvenc encoder. Cached."""
    global _NVENC_CACHE
    if _NVENC_CACHE is not None:
        return _NVENC_CACHE
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=20,
        )
        _NVENC_CACHE = "h264_nvenc" in (result.stdout or "")
    except Exception:
        _NVENC_CACHE = False
    return _NVENC_CACHE


def video_encode_args(ffmpeg: str, gpu: str = "auto") -> list[str]:
    """Encoder flags: GPU (NVENC) when available/requested, else CPU x264.
    gpu: 'auto' (use GPU if present), 'on' (force GPU), 'off' (force CPU)."""
    pref = (gpu or "auto").strip().lower()
    use_gpu = pref == "on" or (pref == "auto" and nvenc_available(ffmpeg))
    if use_gpu:
        # NVENC constant-quality VBR (-cq), quality preset p5, hq tuning.
        return [
            "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
            "-rc", "vbr", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p",
        ]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]


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
    enc_args: list[str] | None = None,
    log_callback: Callable | None = None,
) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Default to CPU x264 if the caller didn't resolve an encoder (keeps old callers working).
    enc_args = enc_args or ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
    filters = [
        f"fps={FPS}",
        # Fill the 1920x1080 frame and crop the overflow, instead of fitting +
        # padding. Generated media isn't exactly 16:9 (LTX 1920x1088, Krea
        # 1928x1088 — height rounded to a multiple of 8), so "fit + pad" left thin
        # side bars. "fill + crop" removes the bars at the cost of a few invisible
        # edge pixels.
        f"scale={VIDEO_SIZE}:force_original_aspect_ratio=increase",
        f"crop={VIDEO_SIZE}",
        # Hold the last frame to reach the target duration: this is how a clip is
        # kept on screen until the next one starts (fills timeline gaps, no black).
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
            "-an", *enc_args, "-r", FPS, "-movflags", "+faststart",
            str(dst),
        ]
    else:
        cmd = [
            ffmpeg, "-y", "-i", str(src),
            "-t", str(duration), "-vf", vf,
            "-an", *enc_args, "-r", FPS, "-movflags", "+faststart",
            str(dst),
        ]
    return run_ffmpeg(cmd, log_callback)


def build_normalized_concat_file(
    cards: list[AssetCard],
    exports_dir: Path,
    ffmpeg: str,
    total_end: int,
    gpu: str = "auto",
    log_callback: Callable | None = None,
) -> str:
    segments_dir = exports_dir / "_normalized_segments"
    segment_paths: list[Path] = []
    included = sorted(
        [card for card in cards if card_included(card)],
        key=lambda card: timecode_to_seconds(card.timeline_start or card.start),
    )
    if not included:
        return ""
    # Position-based durations: each clip is shown until the next clip's start;
    # the last clip runs to total_end (the audio length). This makes the video
    # length match the audio and keeps picture in sync, regardless of gaps/overlaps
    # in the shot timecodes.
    starts = [timecode_to_seconds(card.timeline_start or card.start) for card in included]
    intervals = tiled_intervals(starts, total_end)
    enc_args = video_encode_args(ffmpeg, gpu)
    if log_callback:
        log_callback("Video encoder: " + ("GPU h264_nvenc" if "h264_nvenc" in enc_args else "CPU libx264"))
    seg_index = 0
    for card, (start_sec, end_sec) in zip(included, intervals):
        src = card_source(card)
        if not src:
            continue
        seg_index += 1
        duration = max(1, end_sec - start_sec)
        dst = segments_dir / f"segment_{seg_index:03d}_shot_{card.shot_index:03d}.mp4"
        if log_callback:
            log_callback(f"Normalizing shot {card.shot_index:03d}: {duration}s at t={start_sec}s")
        if not normalize_segment(ffmpeg, src, dst, duration, card, enc_args, log_callback):
            return ""
        segment_paths.append(dst)
    return build_concat_file_from_paths(segment_paths, exports_dir)


MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


def find_music_track(project_dir: Path) -> Path | None:
    """Newest audio file in the project's music/ folder (the ACE-Step bed)."""
    music_dir = project_dir / "music"
    if not music_dir.exists():
        return None
    tracks = sorted(
        [p for p in music_dir.iterdir() if p.is_file() and p.suffix.lower() in MUSIC_EXTENSIONS],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return tracks[0] if tracks else None


def resolve_music_track(project_dir: Path) -> Path | None:
    """The track to use in the export: the explicitly selected one from the
    library if it still exists, otherwise the newest track."""
    meta = project_dir / ".project.json"
    if meta.exists():
        try:
            selected = str(json.loads(meta.read_text(encoding="utf-8")).get("music_selected", "")).strip()
        except Exception:
            selected = ""
        if selected:
            candidate = project_dir / "music" / selected
            if candidate.exists() and candidate.is_file():
                return candidate
    return find_music_track(project_dir)


def _selected_track_trim(project_dir: Path, track_name: str) -> tuple[float, float]:
    tj = project_dir / "music" / "tracks.json"
    if tj.exists():
        try:
            info = json.loads(tj.read_text(encoding="utf-8")).get(track_name, {})
            return float(info.get("trim_start", 0) or 0), float(info.get("trim_end", 0) or 0)
        except Exception:
            return 0.0, 0.0
    return 0.0, 0.0


def prepare_music_input(project_dir: Path, ffmpeg: str, exports_dir: Path,
                        log_callback: Callable | None = None) -> Path | None:
    """The music file to loop into the export. If the selected track has a chosen
    region (trim_start/trim_end), cut just that segment to a temp file and loop it;
    otherwise use the whole track."""
    track = resolve_music_track(project_dir)
    if not track:
        return None
    start, end = _selected_track_trim(project_dir, track.name)
    if end > start and (end - start) >= 1:
        segment = exports_dir / "_music_segment.wav"
        cmd = [
            ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(track),
            "-t", f"{end - start:.3f}", "-vn", "-c:a", "pcm_s16le", str(segment),
        ]
        if log_callback:
            log_callback(f"Trimming music to region {start:.1f}s–{end:.1f}s")
        if run_ffmpeg(cmd, log_callback) and segment.exists():
            return segment
    return track


def read_music_settings(project_dir: Path) -> dict:
    cfg = {"music_enabled": True, "music_volume_db": -18.0, "music_duck": True}
    meta = project_dir / ".project.json"
    if meta.exists():
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            cfg["music_enabled"] = bool(m.get("music_enabled", True))
            cfg["music_volume_db"] = float(m.get("music_volume_db", -18))
            cfg["music_duck"] = bool(m.get("music_duck", True))
        except Exception:
            pass
    return cfg


def _music_filtergraph(voice_start: int, total_end: int, vol_db: float, duck: bool) -> str:
    """filter_complex for input 1 = voice, input 2 = looped music. Music is
    gentle-faded in/out and mixed under the voice; with duck it is side-chained
    to the voice so it dips while the narrator speaks."""
    voice_pre = (
        f"[1:a]adelay={int(voice_start) * 1000}:all=1[voc]"
        if voice_start else "[1:a]anull[voc]"
    )
    fade_out_at = max(0, int(total_end) - 3)
    music_pre = (
        f"[2:a]volume={vol_db}dB,afade=t=in:st=0:d=2,"
        f"afade=t=out:st={fade_out_at}:d=3[mus0]"
    )
    if duck:
        return (
            f"{voice_pre};[voc]asplit=2[vocm][vkey];{music_pre};"
            "[mus0][vkey]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=350[mduck];"
            "[vocm][mduck]amix=inputs=2:normalize=0[a]"
        )
    return f"{voice_pre};{music_pre};[voc][mus0]amix=inputs=2:normalize=0[a]"


def export_video(
    project_dir: Path,
    ffmpeg_path: str = "ffmpeg",
    log_callback: Callable | None = None,
    output_name: str = "final_cut.mp4",
    gpu: str = "auto",
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

    # Total timeline length. With voice, the video must run to the end of the voice
    # track (offset + audio length), so the picture stays in sync and the audio is
    # not cut short. Without voice, fall back to the last clip's end.
    max_card_end = max(timecode_to_seconds(c.timeline_end or c.end) for c in included)
    if voice_audio:
        audio_len = media_duration_seconds(voice_audio, ffmpeg)
        total_end = math.ceil(voice_start + audio_len) if audio_len else max_card_end
    else:
        total_end = max_card_end

    if voice_audio:
        if log_callback:
            log_callback(f"Using voice audio: {voice_audio}")
            if voice_start:
                log_callback(f"Voice timeline offset: {voice_start}s")
        concat_file = build_normalized_concat_file(cards, exports_dir, ffmpeg, total_end, gpu, log_callback)
    elif overlays_enabled:
        if log_callback:
            log_callback("No voice audio found; exporting normalized video with timeline text overlays.")
        concat_file = build_normalized_concat_file(cards, exports_dir, ffmpeg, total_end, gpu, log_callback)
    else:
        if log_callback:
            log_callback("No voice audio found in project voice folder; exporting generated clip audio only.")
        concat_file = build_concat_file(cards, exports_dir)

    if not concat_file:
        if log_callback:
            log_callback("No generated outputs found for included cards.")
        return None

    music_track = prepare_music_input(project_dir, ffmpeg, exports_dir, log_callback)
    music_cfg = read_music_settings(project_dir)
    use_music = bool(music_track) and music_cfg["music_enabled"]

    if voice_audio and use_music:
        if log_callback:
            log_callback(
                f"Mixing background music: {music_track.name} "
                f"({music_cfg['music_volume_db']:.0f} dB{', ducked' if music_cfg['music_duck'] else ''})"
            )
        filtergraph = _music_filtergraph(
            voice_start, total_end, music_cfg["music_volume_db"], music_cfg["music_duck"]
        )
        cmd = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-i", str(voice_audio),
            "-stream_loop", "-1", "-i", str(music_track),
            "-filter_complex", filtergraph,
            "-map", "0:v:0", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(output_path),
        ]
    elif voice_audio:
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


