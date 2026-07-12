"""Technical quality checks for rendered deliverables."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from core.export_profiles import ExportProfile, ExportProfileName, resolve_export_profile
from core.process import resolve_executable


def _ffprobe_path(ffmpeg: str) -> str:
    configured = Path(resolve_executable(ffmpeg, "ffmpeg.exe", "ffmpeg"))
    for name in ("ffprobe.exe", "ffprobe"):
        sibling = configured.with_name(name)
        if sibling.is_file():
            return str(sibling.resolve())
    return resolve_executable("ffprobe", "ffprobe.exe")


def _fraction(value: str) -> float:
    try:
        numerator, denominator = str(value).split("/", 1)
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def evaluate_probe(
    probe: dict[str, Any],
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
    expected_fps: float | None = None,
    require_audio: bool = True,
    profile: ExportProfile | ExportProfileName | str | None = None,
) -> dict[str, Any]:
    """Evaluate ffprobe JSON without performing I/O (keeps policy testable)."""
    selected = resolve_export_profile(profile)
    expected_width = selected.width if expected_width is None else expected_width
    expected_height = selected.height if expected_height is None else expected_height
    expected_fps = float(selected.fps) if expected_fps is None else expected_fps
    # A named profile is an explicit contract.  Legacy callers without one keep
    # codec/pixel-format deviations as compatibility warnings.
    contract_level = "error" if profile is not None else "warning"
    streams = probe.get("streams") if isinstance(probe, dict) else []
    streams = streams if isinstance(streams, list) else []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    issues: list[dict[str, Any]] = []

    def issue(level: str, code: str, message: str, **details: Any) -> None:
        issues.append({"level": level, "code": code, "message": message, "details": details or None})

    if not video:
        issue("error", "missing_video", "Итоговый файл не содержит видеодорожку")
    else:
        width = _integer(video.get("width"))
        height = _integer(video.get("height"))
        fps = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0")
        if width != expected_width or height != expected_height:
            issue(
                "error", "bad_resolution",
                f"Разрешение {width}×{height}, ожидалось {expected_width}×{expected_height}",
                width=width, height=height,
            )
        if not fps or abs(fps - expected_fps) > 0.05:
            issue("error", "bad_fps", f"FPS {fps:.3f}, ожидалось {expected_fps:g}", fps=fps)
        if str(video.get("codec_name", "")).lower() != selected.video_codec:
            issue(
                contract_level,
                "video_codec",
                f"Видеокодек {video.get('codec_name') or 'не определён'}, ожидался {selected.video_codec}",
                codec=video.get("codec_name"), expected=selected.video_codec,
            )
        pix_fmt = str(video.get("pix_fmt", ""))
        if pix_fmt and pix_fmt != selected.pixel_format:
            issue(
                contract_level,
                "pixel_format",
                f"Pixel format {pix_fmt}, ожидался {selected.pixel_format}",
                pix_fmt=pix_fmt, expected=selected.pixel_format,
            )
        video_bitrate = _integer(video.get("bit_rate"))
        max_video_bitrate = (selected.max_video_bitrate_kbps or 0) * 1000
        if video_bitrate and max_video_bitrate and video_bitrate > max_video_bitrate * 1.1:
            issue(
                contract_level,
                "video_bitrate",
                f"Средний видеобитрейт {video_bitrate // 1000} kbps превышает профильный потолок "
                f"{selected.max_video_bitrate_kbps} kbps",
                bitrate=video_bitrate,
                max_bitrate=max_video_bitrate,
            )

    if require_audio and not audio:
        issue("error", "missing_audio", "Итоговый файл не содержит аудиодорожку")
    elif audio:
        audio_codec = str(audio.get("codec_name", "")).lower()
        if audio_codec and audio_codec != selected.audio_codec:
            issue(
                contract_level,
                "audio_codec",
                f"Аудиокодек {audio_codec}, ожидался {selected.audio_codec}",
                codec=audio_codec, expected=selected.audio_codec,
            )
        sample_rate = _integer(audio.get("sample_rate"))
        if sample_rate and sample_rate != selected.audio_sample_rate:
            issue(
                contract_level,
                "audio_sample_rate",
                f"Частота аудио {sample_rate} Hz, ожидалось {selected.audio_sample_rate} Hz",
                sample_rate=sample_rate, expected=selected.audio_sample_rate,
            )
        channels = _integer(audio.get("channels"))
        if channels and channels != selected.audio_channels:
            issue(
                contract_level,
                "audio_channels",
                f"Аудиоканалов {channels}, ожидалось {selected.audio_channels}",
                channels=channels, expected=selected.audio_channels,
            )
        audio_bitrate = _integer(audio.get("bit_rate"))
        expected_audio_bitrate = selected.audio_bitrate_kbps * 1000
        if (
            audio_bitrate
            and abs(audio_bitrate - expected_audio_bitrate) / expected_audio_bitrate > 0.25
        ):
            issue(
                contract_level,
                "audio_bitrate",
                f"Аудиобитрейт {audio_bitrate // 1000} kbps, ожидалось около "
                f"{selected.audio_bitrate_kbps} kbps",
                bitrate=audio_bitrate, expected=expected_audio_bitrate,
            )

    try:
        duration = float((probe.get("format") or {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        issue("error", "bad_duration", "Не удалось определить положительную длительность файла")

    counts = {
        "error": sum(item["level"] == "error" for item in issues),
        "warning": sum(item["level"] == "warning" for item in issues),
    }
    return {
        "ok": counts["error"] == 0,
        "counts": counts,
        "duration": duration,
        "profile": selected.name,
        "expected": {
            "width": expected_width,
            "height": expected_height,
            "fps": expected_fps,
            "video_codec": selected.video_codec,
            "pixel_format": selected.pixel_format,
            "audio_codec": selected.audio_codec,
            "audio_bitrate_kbps": selected.audio_bitrate_kbps,
            "audio_sample_rate": selected.audio_sample_rate,
            "audio_channels": selected.audio_channels,
            "video_bitrate_kbps": selected.video_bitrate_kbps,
            "max_video_bitrate_kbps": selected.max_video_bitrate_kbps,
        },
        "issues": issues,
    }


def check_final_media(
    path: Path,
    ffmpeg: str = "ffmpeg",
    require_audio: bool = True,
    profile: ExportProfile | ExportProfileName | str | None = None,
) -> dict[str, Any]:
    selected = resolve_export_profile(profile)
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        return {
            "ok": False,
            "counts": {"error": 1, "warning": 0},
            "duration": 0.0,
            "profile": selected.name,
            "issues": [{
                "level": "error", "code": "missing_file",
                "message": "Итоговый файл отсутствует или пуст", "details": {"path": str(path)},
            }],
        }
    try:
        output = subprocess.check_output(
            [
                _ffprobe_path(ffmpeg), "-v", "error", "-show_streams", "-show_format",
                "-of", "json", str(path),
            ],
            text=True,
            timeout=30,
        )
        probe = json.loads(output)
    except Exception as exc:
        return {
            "ok": False,
            "counts": {"error": 1, "warning": 0},
            "duration": 0.0,
            "profile": selected.name,
            "issues": [{
                "level": "error", "code": "probe_failed",
                "message": f"Техническая проверка файла не выполнена: {exc}", "details": None,
            }],
        }
    report = evaluate_probe(probe, require_audio=require_audio, profile=profile)
    report["path"] = str(path)
    report["size_bytes"] = path.stat().st_size
    return report
