import dataclasses
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from core.models import AssetCard, timecode_to_seconds
from core.config import load_settings
from core.export_profiles import ExportProfile, ExportProfileName, resolve_export_profile
from core.project import atomic_write_text
from core.process import resolve_executable, run_streaming_process
from core import sfx as sfx_lib

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_DEFAULT_PROFILE = resolve_export_profile()
VIDEO_SIZE = _DEFAULT_PROFILE.video_size
FPS = str(_DEFAULT_PROFILE.fps)


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


def video_encode_args(
    ffmpeg: str,
    gpu: str = "auto",
    profile: ExportProfile | ExportProfileName | str | None = None,
) -> list[str]:
    """Encoder flags: GPU (NVENC) when available/requested, else CPU x264.
    gpu: 'auto' (use GPU if present), 'on' (force GPU), 'off' (force CPU)."""
    selected = resolve_export_profile(profile)
    pref = (gpu or "auto").strip().lower()
    use_gpu = pref == "on" or (pref == "auto" and nvenc_available(ffmpeg))
    if use_gpu:
        # All tokens originate from a validated profile, never raw API input.
        args = [
            "-c:v", "h264_nvenc", "-preset", selected.nvenc_preset, "-tune", "hq",
            "-rc", "vbr", "-cq", str(selected.nvenc_cq),
            "-b:v", f"{selected.video_bitrate_kbps}k" if selected.video_bitrate_kbps else "0",
        ]
    else:
        args = [
            "-c:v", "libx264", "-preset", selected.cpu_preset,
            "-crf", str(selected.crf),
        ]
        if selected.video_bitrate_kbps:
            args.extend(["-b:v", f"{selected.video_bitrate_kbps}k"])
    if selected.max_video_bitrate_kbps:
        args.extend([
            "-maxrate", f"{selected.max_video_bitrate_kbps}k",
            "-bufsize", f"{selected.max_video_bitrate_kbps * 2}k",
        ])
    args.extend(["-pix_fmt", selected.pixel_format])
    return args


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


# Routes whose deliverable is a VIDEO file (card.video_output), vs the single
# "image" route that ships a still. Kept as one set so every consumer — the
# exporter, the Master gate and the web route resolver — agrees. `reference`
# (Wikipedia/arXiv insert) produces a video clip, so it belongs with the
# video routes; before this it fell through to "image+video" by luck.
VIDEO_ROUTES = {"stock", "kenburns", "image+video", "reference"}
IMAGE_ROUTES = {"image"}
KNOWN_ROUTES = VIDEO_ROUTES | IMAGE_ROUTES


def _field(card, name: str, default=""):
    """Read a field from either an AssetCard-like object or a plain dict, so
    the exporter and the (dict-based) Master gate share one resolver."""
    if isinstance(card, dict):
        return card.get(name, default)
    return getattr(card, name, default)


def card_route(card) -> str:
    route = str(_field(card, "route", "") or "").lower()
    vt = str(_field(card, "visual_type", "") or "").lower()
    # A reference insert is video-backed even in older boards where only the
    # visual_type was updated and a stale, otherwise-valid route remained.
    # Generation fallbacks synchronise both fields before persisting, so a
    # reference -> Ken Burns fallback still resolves as Ken Burns.
    if route == "reference" or vt == "web_reference_card":
        return "reference"
    if route in KNOWN_ROUTES:
        return route
    if vt == "stock_video":
        return "stock"
    if vt == "still_kenburns":
        return "kenburns"
    if vt == "ai_image":
        return "image"
    return "image+video" if _field(card, "video_model", "") else "image"


def card_source(card) -> Path | None:
    assigned = str(_field(card, "assigned_media", "") or "")
    if assigned and Path(assigned).exists():
        return Path(assigned)

    route = card_route(card)
    if route in VIDEO_ROUTES:
        v = str(_field(card, "video_output", "") or "")
        return Path(v) if v and Path(v).exists() else None
    if route in IMAGE_ROUTES:
        i = str(_field(card, "image_output", "") or "")
        return Path(i) if i and Path(i).exists() else None
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


def find_overlay_font(prefer_mono: bool = False) -> Path | None:
    mono_fonts = (
        Path(r"C:\Windows\Fonts\CascadiaMono.ttf"),
        Path(r"C:\Windows\Fonts\CascadiaMonoPL.ttf"),
        Path(r"C:\Windows\Fonts\consola.ttf"),
    )
    ui_fonts = (
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\tahoma.ttf"),
    )
    for font_path in ((*mono_fonts, *ui_fonts) if prefer_mono else (*ui_fonts, *mono_fonts)):
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
        "stat": {
            "fontsize": 78, "borderw": 3, "shadowx": 3, "shadowy": 3, "default_y": 16,
            "x": "w*0.07", "box": "black@0.34", "boxborderw": 18,
        },
        "terminal": {
            "fontsize": 36, "borderw": 1, "shadowx": 2, "shadowy": 2, "default_y": 14,
            "x": "w*0.07", "mono": True, "box": "black@0.46", "boxborderw": 14,
        },
        "badge": {
            "fontsize": 42, "borderw": 2, "shadowx": 2, "shadowy": 2, "default_y": 16,
            "x": "w*0.07", "box": "0x0f766e@0.50", "boxborderw": 16,
        },
        "warning": {
            "fontsize": 48, "borderw": 3, "shadowx": 2, "shadowy": 2, "default_y": 12,
            "x": "(w-text_w)/2", "box": "0x991b1b@0.58", "boxborderw": 16,
        },
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
    if position == "left":
        return "w*0.07", "(h-text_h)/2"
    if position == "right":
        return "w-text_w-w*0.07", "(h-text_h)/2"
    if position == "top_left":
        return "w*0.07", "h*0.10"
    if position == "top_right":
        return "w-text_w-w*0.07", "h*0.10"
    if position == "bottom_left":
        return "w*0.07", "h*0.80"
    if position == "bottom_right":
        return "w-text_w-w*0.07", "h*0.80"
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
    font = find_overlay_font(prefer_mono=bool(style_cfg.get("mono")))
    font_part = f"fontfile='{ffmpeg_filter_path(font)}':" if font else ""
    text_file = ffmpeg_filter_path(text_path)
    alpha = _overlay_alpha(card).replace(",", r"\,")
    box_part = ""
    if style_cfg.get("box"):
        box_part = f":box=1:boxcolor={style_cfg['box']}:boxborderw={style_cfg.get('boxborderw', 12)}"
    return (
        f"drawtext={font_part}textfile='{text_file}':fontcolor=white:"
        f"fontsize={style_cfg['fontsize']}:borderw={style_cfg['borderw']}:bordercolor=black@0.72:"
        f"shadowx={style_cfg['shadowx']}:shadowy={style_cfg['shadowy']}:shadowcolor=black@0.55:"
        f"x={x_expr}:y={y_expr}:alpha='{alpha}'{box_part}"
    )


def has_text_overlays(cards: list[AssetCard]) -> bool:
    return any(card_overlay_text(card) for card in cards if card_included(card))


def run_ffmpeg(cmd: list[str], log_callback: Callable | None = None) -> bool:
    if log_callback:
        log_callback(f"Running: {' '.join(cmd)}")
    try:
        code = run_streaming_process(cmd, log_callback)
    except FileNotFoundError:
        if log_callback:
            log_callback(f"ffmpeg not found: {cmd[0]}")
        return False
    if code != 0:
        if log_callback:
            log_callback(f"FFmpeg exited with code {code}")
        return False
    return True


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


def unify_finish_filters(settings) -> list[str]:
    """Optional finishing pass baked into every export segment: one shared mild
    grade plus temporal film grain. Stock, Krea stills and LTX clips come from
    different 'cameras' — a common finish makes them read as one production, and
    living grain over tpad-held frames keeps static shots from looking frozen."""
    filters: list[str] = []
    if getattr(settings, "export_unify_grade", False):
        filters.append("eq=contrast=1.05:saturation=1.05:gamma=0.99")
        grain = int(getattr(settings, "export_grain", 0) or 0)
        if grain > 0:
            grain = min(grain, 20)
            filters.append(f"noise=alls={grain}:allf=t")
    return filters


def normalize_segment(
    ffmpeg: str,
    src: Path,
    dst: Path,
    duration: int,
    card: AssetCard,
    enc_args: list[str] | None = None,
    log_callback: Callable | None = None,
    finish_filters: list[str] | None = None,
    profile: ExportProfile | ExportProfileName | str | None = None,
) -> bool:
    selected = resolve_export_profile(profile)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Default to CPU x264 if the caller didn't resolve an encoder (keeps old callers working).
    enc_args = enc_args or video_encode_args(ffmpeg, "off", selected)
    filters = [
        f"fps={selected.fps}",
        # Fill the profile frame and crop the overflow, instead of fitting +
        # padding. Generated media isn't exactly 16:9 (LTX 1920x1088, Krea
        # 1928x1088 — height rounded to a multiple of 8), so "fit + pad" left thin
        # side bars. "fill + crop" removes the bars at the cost of a few invisible
        # edge pixels.
        f"scale={selected.video_size}:force_original_aspect_ratio=increase",
        f"crop={selected.video_size}",
        # Hold the last frame to reach the target duration: this is how a clip is
        # kept on screen until the next one starts (fills timeline gaps, no black).
        f"tpad=stop_mode=clone:stop_duration={duration}",
    ]
    overlay = overlay_filter(card, dst)
    if overlay:
        if log_callback:
            log_callback(f"Applying text overlay to shot {card.shot_index:03d}: {card_overlay_text(card)}")
        filters.append(overlay)
    # Finishing pass LAST so grade/grain cover the overlay and tpad-held frames.
    if finish_filters:
        filters.extend(finish_filters)
    vf = ",".join(filters)
    if src.suffix.lower() in IMAGE_EXTENSIONS:
        cmd = [
            ffmpeg, "-y", "-loop", "1", "-i", str(src),
            "-t", str(duration), "-vf", vf,
            "-an", *enc_args, "-r", str(selected.fps), "-movflags", "+faststart",
            str(dst),
        ]
    else:
        cmd = [
            ffmpeg, "-y", "-i", str(src),
            "-t", str(duration), "-vf", vf,
            "-an", *enc_args, "-r", str(selected.fps), "-movflags", "+faststart",
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
    finish_filters: list[str] | None = None,
    profile: ExportProfile | ExportProfileName | str | None = None,
) -> str:
    selected = resolve_export_profile(profile)
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
    enc_args = video_encode_args(ffmpeg, gpu, selected)
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
        if not normalize_segment(
            ffmpeg, src, dst, duration, card, enc_args, log_callback, finish_filters, selected,
        ):
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
                        log_callback: Callable | None = None,
                        normalize: bool = False) -> Path | None:
    """The music file to loop into the export. If the selected track has a chosen
    region (trim_start/trim_end), cut just that segment to a temp file and loop it;
    otherwise use the whole track. With `normalize`, the result is loudness-
    normalized to a fixed level first (generated ACE-Step tracks vary a lot), so
    the music_volume_db offset lands at the same perceived loudness every time."""
    track = resolve_music_track(project_dir)
    if not track:
        return None
    start, end = _selected_track_trim(project_dir, track.name)
    result: Path = track
    if end > start and (end - start) >= 1:
        segment = exports_dir / "_music_segment.wav"
        cmd = [
            ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(track),
            "-t", f"{end - start:.3f}", "-vn", "-c:a", "pcm_s16le", str(segment),
        ]
        if log_callback:
            log_callback(f"Trimming music to region {start:.1f}s–{end:.1f}s")
        if run_ffmpeg(cmd, log_callback) and segment.exists():
            result = segment
    if normalize:
        prepared = exports_dir / "_music_prepared.wav"
        cmd = [
            ffmpeg, "-y", "-i", str(result),
            "-af", "loudnorm=I=-20:TP=-2:LRA=11",
            "-vn", "-c:a", "pcm_s16le", str(prepared),
        ]
        if log_callback:
            log_callback("Normalizing music loudness to -20 LUFS")
        if run_ffmpeg(cmd, log_callback) and prepared.exists():
            return prepared
    return result


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


# YouTube-targeted final-mix loudness (see export_loudnorm setting).
LOUDNORM_FILTER = "loudnorm=I=-14:TP=-1.5:LRA=11"


def _audio_mix_filtergraph(
    voice_start: int,
    total_end: int,
    vol_db: float,
    duck: bool,
    music_index: int | None = None,
    sfx_index: int | None = None,
    loudnorm: bool = False,
) -> str:
    """filter_complex mixing input 1 (voice) with optional looped music and an
    optional pre-leveled SFX bed. Music is gentle-faded and, with duck, side-
    chained to the voice so it dips while the narrator speaks. The SFX bed comes
    in already delayed/leveled (see core.sfx.build_sfx_bed), so it is mixed as-is.
    With `loudnorm`, the final mix is normalized to the YouTube target (-14 LUFS)."""
    # Pin every branch to one format up front: sidechaincompress/amix refuse to
    # negotiate when parallel branches disagree (e.g. mono voice + stereo bed).
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    parts: list[str] = []
    voice_pre = (
        f"[1:a]{fmt},adelay={int(voice_start) * 1000}:all=1[voc]"
        if voice_start else f"[1:a]{fmt}[voc]"
    )
    parts.append(voice_pre)
    mix_labels: list[str] = []

    if music_index is not None:
        fade_out_at = max(0, int(total_end) - 3)
        music_pre = (
            f"[{music_index}:a]{fmt},volume={vol_db}dB,afade=t=in:st=0:d=2,"
            f"afade=t=out:st={fade_out_at}:d=3[mus0]"
        )
        if duck:
            parts.append("[voc]asplit=2[vocm][vkey]")
            parts.append(music_pre)
            parts.append(
                "[mus0][vkey]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=350[mduck]"
            )
            mix_labels.extend(["[vocm]", "[mduck]"])
        else:
            parts.append(music_pre)
            mix_labels.extend(["[voc]", "[mus0]"])
    else:
        mix_labels.append("[voc]")

    if sfx_index is not None:
        parts.append(f"[{sfx_index}:a]{fmt}[sfxbed]")
        mix_labels.append("[sfxbed]")

    # amix normalize=0 sums the branches without headroom, so on peaks the voice
    # + music (+ sfx) sum can exceed 0 dBFS and clip the AAC encode. A safety
    # limiter on the bus caps it at ~-0.5 dBFS; level=false means it only pulls
    # peaks down (no makeup gain), so quiet mixes are untouched. Optional
    # loudnorm runs first, the limiter always last as the final guarantee.
    safety = ",alimiter=limit=0.94:level=false"
    tail = (f",{LOUDNORM_FILTER}" if loudnorm else "") + safety
    if len(mix_labels) == 1:
        parts.append(f"{mix_labels[0]}anull{tail}[a]")
    else:
        parts.append(
            "".join(mix_labels) + f"amix=inputs={len(mix_labels)}:normalize=0{tail}[a]"
        )
    return ";".join(parts)


def _normalize_without_voice(overlays_enabled: bool, profile_was_explicit: bool) -> bool:
    """An explicit named profile is a delivery contract, even without voice.

    Legacy GUI callers that omit ``profile`` retain their concat-copy fast path;
    API exports always pass a profile and therefore always enforce its geometry,
    frame rate and codec.
    """
    return bool(overlays_enabled or profile_was_explicit)


def export_video(
    project_dir: Path,
    ffmpeg_path: str = "ffmpeg",
    log_callback: Callable | None = None,
    output_name: str = "final_cut.mp4",
    gpu: str = "auto",
    profile: ExportProfile | ExportProfileName | str | None = None,
    runtime_settings: Any | None = None,
    generate_missing_sfx: bool = True,
) -> Path | None:
    """Export the final video by concatenating included card outputs (media present, not excluded)."""
    profile_was_explicit = profile is not None
    selected_profile = resolve_export_profile(profile)
    # Versioned exports: the default name gets a timestamp so re-exports don't
    # overwrite each other and cuts can be compared (v1/v2/v3 workflow). An
    # explicit output_name is respected as-is.
    if output_name == "final_cut.mp4":
        from datetime import datetime
        output_name = f"final_cut_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
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

    ffmpeg = resolve_executable(ffmpeg_path, "ffmpeg.exe", "ffmpeg")
    settings = runtime_settings or load_settings()
    if log_callback:
        log_callback(
            f"Export profile: {selected_profile.label} "
            f"({selected_profile.width}x{selected_profile.height}, {selected_profile.fps} fps, "
            f"{selected_profile.video_codec.upper()}/{selected_profile.audio_codec.upper()})"
        )
    finish_filters = unify_finish_filters(settings)
    if finish_filters and log_callback:
        log_callback("Unifying finish: shared grade + film grain on every segment")
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
        concat_file = build_normalized_concat_file(
            cards, exports_dir, ffmpeg, total_end, gpu, log_callback, finish_filters,
            selected_profile,
        )
    elif _normalize_without_voice(overlays_enabled, profile_was_explicit):
        if log_callback:
            reason = "timeline text overlays" if overlays_enabled else "the selected profile"
            log_callback(f"No voice audio found; normalizing video for {reason}.")
        concat_file = build_normalized_concat_file(
            cards, exports_dir, ffmpeg, total_end, gpu, log_callback, finish_filters,
            selected_profile,
        )
    else:
        if log_callback:
            log_callback("No voice audio found in project voice folder; exporting generated clip audio only.")
        concat_file = build_concat_file(cards, exports_dir)

    if not concat_file:
        if log_callback:
            log_callback("No generated outputs found for included cards.")
        return None

    music_track = prepare_music_input(
        project_dir, ffmpeg, exports_dir, log_callback,
        normalize=bool(getattr(settings, "music_normalize", False)),
    )
    music_cfg = read_music_settings(project_dir)
    use_music = bool(music_track) and music_cfg["music_enabled"]

    # SFX bed (optional, Woosh): per-shot ambience mixed under the voice at the
    # same tiled intervals the picture uses. Any failure degrades to "no SFX".
    sfx_bed: Path | None = None
    if voice_audio and sfx_lib.sfx_enabled(settings):
        try:
            if (
                generate_missing_sfx
                and sfx_lib.ensure_sfx_for_cards(
                    project_dir, included, settings, log_callback
                )
            ):
                try:
                    atomic_write_text(
                        board_path,
                        json.dumps([dataclasses.asdict(c) for c in cards], ensure_ascii=False, indent=2),
                    )
                except OSError:
                    pass
            starts = [timecode_to_seconds(c.timeline_start or c.start) for c in included]
            intervals = tiled_intervals(starts, total_end)
            sfx_bed = sfx_lib.build_sfx_bed(
                list(zip(included, intervals)), exports_dir, ffmpeg, total_end,
                volume_db=float(getattr(settings, "sfx_volume_db", -24.0)),
                log=log_callback,
            )
        except Exception as exc:
            if log_callback:
                log_callback(f"SFX layer skipped: {exc}")
            sfx_bed = None

    loudnorm = bool(getattr(settings, "export_loudnorm", False))

    if voice_audio and (use_music or sfx_bed or loudnorm):
        if use_music and log_callback:
            log_callback(
                f"Mixing background music: {music_track.name} "
                f"({music_cfg['music_volume_db']:.0f} dB{', ducked' if music_cfg['music_duck'] else ''})"
            )
        if sfx_bed and log_callback:
            log_callback(f"Mixing SFX bed: {sfx_bed.name} ({float(getattr(settings, 'sfx_volume_db', -24.0)):.0f} dB)")
        if loudnorm and log_callback:
            log_callback("Final mix loudness: normalizing to -14 LUFS")
        cmd = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-i", str(voice_audio),
        ]
        next_index = 2
        music_index: int | None = None
        sfx_index: int | None = None
        if use_music:
            cmd += ["-stream_loop", "-1", "-i", str(music_track)]
            music_index = next_index
            next_index += 1
        if sfx_bed:
            cmd += ["-i", str(sfx_bed)]
            sfx_index = next_index
            next_index += 1
        filtergraph = _audio_mix_filtergraph(
            voice_start, total_end,
            music_cfg["music_volume_db"], music_cfg["music_duck"],
            music_index=music_index, sfx_index=sfx_index, loudnorm=loudnorm,
        )
        cmd += [
            "-filter_complex", filtergraph,
            "-map", "0:v:0", "-map", "[a]",
            "-c:v", "copy", "-c:a", selected_profile.audio_codec,
            "-b:a", selected_profile.audio_bitrate,
            "-ar", str(selected_profile.audio_sample_rate),
            "-ac", str(selected_profile.audio_channels),
            "-shortest",
            str(output_path),
        ]
    elif voice_audio:
        cmd = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-i", str(voice_audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", selected_profile.audio_codec,
            "-b:a", selected_profile.audio_bitrate,
            "-ar", str(selected_profile.audio_sample_rate),
            "-ac", str(selected_profile.audio_channels),
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
