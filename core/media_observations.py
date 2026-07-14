"""Deterministic, policy-free observations for generated image and video files.

The functions in this module answer only technical questions: can the file be
read, what media stream does it contain, how long is it, and which portions are
reported as black or frozen by FFmpeg.  They deliberately do not decide whether
an asset should be accepted, reviewed, or regenerated.

The two stable integration entry points are :func:`observe_image` and
:func:`observe_video`.  Lower-level evaluators and detector parsers are kept
pure so threshold policy can be tested independently of subprocess execution.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from core.process import resolve_executable


DEFAULT_DURATION_ABSOLUTE_TOLERANCE = 0.25
DEFAULT_DURATION_RELATIVE_TOLERANCE = 0.10
DEFAULT_PROBE_TIMEOUT = 30
DEFAULT_DECODE_TIMEOUT = 120
DEFAULT_DETECT_TIMEOUT = 120

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_DETECTOR_TOKEN = re.compile(
    rf"(?P<kind>black|freeze)_(?P<event>start|end|duration)\s*[:=]\s*(?P<value>{_NUMBER})",
    re.IGNORECASE,
)


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "details": details or None,
    }


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _positive_float(*values: Any) -> float:
    for value in values:
        parsed = _finite_float(value)
        if parsed > 0:
            return parsed
    return 0.0


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fraction(value: Any) -> float:
    text = str(value or "0").strip()
    try:
        numerator, denominator = text.split("/", 1)
        parsed = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        parsed = _finite_float(text)
    return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0


def _error_tail(value: Any, limit: int = 4000) -> str:
    text = str(value or "").strip()
    return text[-limit:]


def evaluate_duration(
    actual_duration: Any,
    expected_duration: float | None,
    *,
    absolute_tolerance: float = DEFAULT_DURATION_ABSOLUTE_TOLERANCE,
    relative_tolerance: float = DEFAULT_DURATION_RELATIVE_TOLERANCE,
) -> dict[str, Any]:
    """Compare durations without assigning an editorial action.

    Tolerance is the larger of an absolute floor and a fraction of the expected
    duration.  A missing or non-positive expectation leaves the check disabled.
    """

    actual = max(0.0, _finite_float(actual_duration))
    expected = _finite_float(expected_duration) if expected_duration is not None else 0.0
    if expected <= 0:
        return {
            "checked": False,
            "actual": actual,
            "expected": None,
            "delta": None,
            "absolute_delta": None,
            "tolerance": None,
            "within_tolerance": None,
        }

    tolerance = max(
        0.0,
        _finite_float(absolute_tolerance),
        expected * max(0.0, _finite_float(relative_tolerance)),
    )
    delta = actual - expected
    absolute_delta = abs(delta)
    return {
        "checked": True,
        "actual": actual,
        "expected": expected,
        "delta": delta,
        "absolute_delta": absolute_delta,
        "tolerance": tolerance,
        "within_tolerance": absolute_delta <= tolerance + 1e-9,
    }


def evaluate_video_probe(
    probe: dict[str, Any] | Any,
    *,
    expected_duration: float | None = None,
    absolute_tolerance: float = DEFAULT_DURATION_ABSOLUTE_TOLERANCE,
    relative_tolerance: float = DEFAULT_DURATION_RELATIVE_TOLERANCE,
) -> dict[str, Any]:
    """Turn ffprobe JSON into stable, JSON-compatible video observations."""

    data = probe if isinstance(probe, dict) else {}
    raw_streams = data.get("streams", [])
    streams = raw_streams if isinstance(raw_streams, list) else []
    video = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and str(stream.get("codec_type", "")).lower() == "video"
        ),
        None,
    )
    format_data = data.get("format") if isinstance(data.get("format"), dict) else {}
    issues: list[dict[str, Any]] = []

    if video is None:
        issues.append(_issue("missing_video_stream", "FFprobe не обнаружил видеопоток"))
        width = height = frame_count = 0
        fps = 0.0
        codec_name = pixel_format = ""
        stream_duration = 0.0
    else:
        width = _integer(video.get("width"))
        height = _integer(video.get("height"))
        frame_count = _integer(video.get("nb_read_frames") or video.get("nb_frames"))
        fps = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        codec_name = str(video.get("codec_name", "") or "")
        pixel_format = str(video.get("pix_fmt", "") or "")
        stream_duration = _positive_float(video.get("duration"))
        if width <= 0 or height <= 0:
            issues.append(
                _issue(
                    "invalid_dimensions",
                    "Видеопоток не содержит положительных размеров кадра",
                    width=width,
                    height=height,
                )
            )

    duration = _positive_float(format_data.get("duration"), stream_duration)
    duration_check = evaluate_duration(
        duration,
        expected_duration,
        absolute_tolerance=absolute_tolerance,
        relative_tolerance=relative_tolerance,
    )
    if duration <= 0:
        issues.append(_issue("invalid_duration", "FFprobe не вернул положительную длительность"))
    elif duration_check["checked"] and not duration_check["within_tolerance"]:
        issues.append(
            _issue(
                "duration_mismatch",
                "Длительность видео выходит за допустимое отклонение",
                actual=duration,
                expected=duration_check["expected"],
                tolerance=duration_check["tolerance"],
                absolute_delta=duration_check["absolute_delta"],
            )
        )

    return {
        "ok": not issues,
        "issues": issues,
        "has_video": video is not None,
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "frame_count": frame_count,
        "codec_name": codec_name,
        "pixel_format": pixel_format,
        "duration_check": duration_check,
    }


def normalize_intervals(
    intervals: Iterable[tuple[float, float] | dict[str, Any]],
    duration: float,
) -> list[dict[str, float]]:
    """Clamp, sort, and merge detector intervals.

    Overlapping and touching ranges are merged before fractions are calculated,
    preventing duplicated FFmpeg log lines from inflating the result.
    """

    total_duration = max(0.0, _finite_float(duration))
    prepared: list[tuple[float, float]] = []
    for interval in intervals:
        if isinstance(interval, dict):
            start = _finite_float(interval.get("start"))
            end = _finite_float(interval.get("end"))
        else:
            try:
                raw_start, raw_end = interval
            except (TypeError, ValueError):
                continue
            start = _finite_float(raw_start)
            end = _finite_float(raw_end)
        start = max(0.0, start)
        end = max(0.0, end)
        if total_duration > 0:
            start = min(start, total_duration)
            end = min(end, total_duration)
        if end > start:
            prepared.append((start, end))

    prepared.sort()
    merged: list[list[float]] = []
    for start, end in prepared:
        if merged and start <= merged[-1][1] + 1e-9:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [
        {"start": start, "end": end, "duration": end - start}
        for start, end in merged
    ]


def interval_fraction(intervals: Iterable[dict[str, Any]], duration: float) -> float:
    """Return the covered fraction after normalising overlapping intervals."""

    total_duration = max(0.0, _finite_float(duration))
    if total_duration <= 0:
        return 0.0
    merged = normalize_intervals(intervals, total_duration)
    covered = sum(interval["duration"] for interval in merged)
    return max(0.0, min(1.0, covered / total_duration))


def _parse_detector(stderr: str, kind: str, duration: float) -> list[dict[str, float]]:
    wanted = kind.lower()
    open_start: float | None = None
    raw_intervals: list[tuple[float, float]] = []

    for token in _DETECTOR_TOKEN.finditer(str(stderr or "")):
        if token.group("kind").lower() != wanted:
            continue
        event = token.group("event").lower()
        value = _finite_float(token.group("value"))
        if event == "start":
            # A repeated start without an end is best represented as two
            # adjacent intervals; normalisation merges them deterministically.
            if open_start is not None and value > open_start:
                raw_intervals.append((open_start, value))
            open_start = value
        elif event == "end" and open_start is not None:
            raw_intervals.append((open_start, value))
            open_start = None
        # Duration messages are intentionally not used to infer boundaries:
        # FFmpeg emits them before/after end depending on build and filter.

    total_duration = max(0.0, _finite_float(duration))
    if open_start is not None and total_duration > open_start:
        raw_intervals.append((open_start, total_duration))
    return normalize_intervals(raw_intervals, total_duration)


def parse_blackdetect(stderr: str, duration: float) -> list[dict[str, float]]:
    """Parse blackdetect log messages, closing a trailing range at EOF."""

    return _parse_detector(stderr, "black", duration)


def parse_freezedetect(stderr: str, duration: float) -> list[dict[str, float]]:
    """Parse freezedetect log messages, closing a trailing freeze at EOF."""

    return _parse_detector(stderr, "freeze", duration)


def evaluate_decode_result(
    returncode: int | None,
    stderr: str = "",
    *,
    timed_out: bool = False,
) -> dict[str, Any]:
    """Pure evaluation of an FFmpeg full-decode process result."""

    issues: list[dict[str, Any]] = []
    if timed_out:
        issues.append(_issue("decode_timeout", "Проверка полного декодирования превысила лимит времени"))
    elif returncode != 0:
        issues.append(
            _issue(
                "decode_failed",
                "FFmpeg обнаружил ошибку при полном декодировании видеопотока",
                returncode=returncode,
                stderr=_error_tail(stderr),
            )
        )
    return {
        "ok": not issues,
        "issues": issues,
        "returncode": returncode,
        "stderr": _error_tail(stderr),
    }


def _resolve_ffmpeg(ffmpeg: str) -> str:
    return resolve_executable(ffmpeg, "ffmpeg.exe", "ffmpeg")


def _resolve_ffprobe(ffmpeg: str) -> str:
    configured = Path(_resolve_ffmpeg(ffmpeg))
    for name in ("ffprobe.exe", "ffprobe"):
        sibling = configured.with_name(name)
        if sibling.is_file():
            return str(sibling.resolve())
    return resolve_executable("ffprobe", "ffprobe.exe", "ffprobe")


def _file_observation(path: Path) -> dict[str, Any]:
    result = {
        "path": str(path),
        "exists": False,
        "nonempty": False,
        "size_bytes": 0,
        "issues": [],
    }
    try:
        if not path.is_file():
            result["issues"].append(_issue("missing_file", "Медиафайл отсутствует", path=str(path)))
            return result
        size = path.stat().st_size
    except OSError as exc:
        result["issues"].append(
            _issue("file_stat_failed", "Не удалось прочитать сведения о медиафайле", error=str(exc))
        )
        return result
    result["exists"] = True
    result["size_bytes"] = size
    result["nonempty"] = size > 0
    if size <= 0:
        result["issues"].append(_issue("empty_file", "Медиафайл пуст", path=str(path)))
    return result


def observe_image(path: Path) -> dict[str, Any]:
    """Verify an image file with Pillow and report its dimensions."""

    image_path = Path(path)
    base = _file_observation(image_path)
    result: dict[str, Any] = {
        **base,
        "ok": False,
        "decode_ok": False,
        "width": 0,
        "height": 0,
        "format": "",
        "mode": "",
    }
    if not base["exists"] or not base["nonempty"]:
        return result

    try:
        with Image.open(image_path) as image:
            result["width"], result["height"] = image.size
            result["format"] = str(image.format or "")
            result["mode"] = str(image.mode or "")
            image.verify()
        if result["width"] <= 0 or result["height"] <= 0:
            result["issues"].append(
                _issue(
                    "invalid_dimensions",
                    "Изображение не содержит положительных размеров",
                    width=result["width"],
                    height=result["height"],
                )
            )
        else:
            result["decode_ok"] = True
    except Exception as exc:
        result["issues"].append(
            _issue("image_decode_failed", "Pillow не смог проверить изображение", error=str(exc))
        )
    result["ok"] = not result["issues"]
    return result


def _run_ffprobe(path: Path, ffmpeg: str) -> dict[str, Any]:
    command = [
        _resolve_ffprobe(ffmpeg),
        "-v",
        "error",
        "-count_frames",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DEFAULT_PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "probe": {},
            "issues": [_issue("probe_timeout", "FFprobe превысил лимит времени")],
        }
    except OSError as exc:
        return {
            "ok": False,
            "probe": {},
            "issues": [_issue("probe_unavailable", "FFprobe не удалось запустить", error=str(exc))],
        }
    if completed.returncode != 0:
        return {
            "ok": False,
            "probe": {},
            "issues": [
                _issue(
                    "probe_failed",
                    "FFprobe не смог прочитать видео",
                    returncode=completed.returncode,
                    stderr=_error_tail(completed.stderr),
                )
            ],
        }
    try:
        probe = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "probe": {},
            "issues": [_issue("probe_invalid_json", "FFprobe вернул невалидный JSON", error=str(exc))],
        }
    if not isinstance(probe, dict):
        return {
            "ok": False,
            "probe": {},
            "issues": [_issue("probe_invalid_json", "FFprobe JSON должен быть объектом")],
        }
    return {"ok": True, "probe": probe, "issues": []}


def _decode_video(path: Path, ffmpeg: str) -> dict[str, Any]:
    command = [
        _resolve_ffmpeg(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DEFAULT_DECODE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return evaluate_decode_result(None, timed_out=True)
    except OSError as exc:
        return {
            "ok": False,
            "issues": [_issue("ffmpeg_unavailable", "FFmpeg не удалось запустить", error=str(exc))],
            "returncode": None,
            "stderr": "",
        }
    return evaluate_decode_result(completed.returncode, completed.stderr)


def _detect_video_intervals(path: Path, ffmpeg: str, duration: float) -> dict[str, Any]:
    command = [
        _resolve_ffmpeg(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        # -30 dB is deliberate: on low-amplitude AI-video motion, -35 dB
        # fragmented long static runs into short false movement gaps.  The
        # threshold remains observational; action policy lives elsewhere.
        "blackdetect=d=0.25:pix_th=0.10,freezedetect=n=-30dB:d=0.5",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DEFAULT_DETECT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "issues": [_issue("detector_timeout", "FFmpeg-анализ black/freeze превысил лимит времени")],
            "black_intervals": [],
            "freeze_intervals": [],
        }
    except OSError as exc:
        return {
            "ok": False,
            "issues": [_issue("ffmpeg_unavailable", "FFmpeg не удалось запустить", error=str(exc))],
            "black_intervals": [],
            "freeze_intervals": [],
        }
    stderr = completed.stderr or ""
    black_intervals = parse_blackdetect(stderr, duration)
    freeze_intervals = parse_freezedetect(stderr, duration)
    issues: list[dict[str, Any]] = []
    if completed.returncode != 0:
        issues.append(
            _issue(
                "detector_failed",
                "FFmpeg не завершил black/freeze-анализ",
                returncode=completed.returncode,
                stderr=_error_tail(stderr),
            )
        )
    return {
        "ok": not issues,
        "issues": issues,
        "black_intervals": black_intervals,
        "freeze_intervals": freeze_intervals,
    }


def observe_video(
    path: Path,
    ffmpeg: str,
    expected_duration: float | None = None,
) -> dict[str, Any]:
    """Collect policy-free ffprobe, decode, black, and freeze observations."""

    video_path = Path(path)
    base = _file_observation(video_path)
    result: dict[str, Any] = {
        **base,
        "ok": False,
        "probe_ok": False,
        "decode_ok": False,
        "detector_ok": False,
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "frame_count": 0,
        "codec_name": "",
        "pixel_format": "",
        "duration_check": evaluate_duration(0.0, expected_duration),
        "black_intervals": [],
        "freeze_intervals": [],
        "black_fraction": 0.0,
        "frozen_fraction": 0.0,
    }
    if not base["exists"] or not base["nonempty"]:
        return result

    probe_run = _run_ffprobe(video_path, ffmpeg)
    result["issues"].extend(probe_run["issues"])
    if not probe_run["ok"]:
        return result

    probe_observation = evaluate_video_probe(
        probe_run["probe"],
        expected_duration=expected_duration,
    )
    result["probe_ok"] = probe_observation["ok"]
    for key in (
        "duration",
        "width",
        "height",
        "fps",
        "frame_count",
        "codec_name",
        "pixel_format",
        "duration_check",
    ):
        result[key] = probe_observation[key]
    result["issues"].extend(probe_observation["issues"])

    # A stream may still be decodable even when duration/dimension metadata is
    # incomplete, so run the independent decode check whenever a video exists.
    if probe_observation["has_video"]:
        decode = _decode_video(video_path, ffmpeg)
        result["decode_ok"] = decode["ok"]
        result["issues"].extend(decode["issues"])

        detectors = _detect_video_intervals(video_path, ffmpeg, result["duration"])
        result["detector_ok"] = detectors["ok"]
        result["issues"].extend(detectors["issues"])
        result["black_intervals"] = detectors["black_intervals"]
        result["freeze_intervals"] = detectors["freeze_intervals"]
        result["black_fraction"] = interval_fraction(
            result["black_intervals"], result["duration"]
        )
        result["frozen_fraction"] = interval_fraction(
            result["freeze_intervals"], result["duration"]
        )

    result["ok"] = not result["issues"]
    return result


__all__ = [
    "DEFAULT_DURATION_ABSOLUTE_TOLERANCE",
    "DEFAULT_DURATION_RELATIVE_TOLERANCE",
    "evaluate_decode_result",
    "evaluate_duration",
    "evaluate_video_probe",
    "interval_fraction",
    "normalize_intervals",
    "observe_image",
    "observe_video",
    "parse_blackdetect",
    "parse_freezedetect",
]
