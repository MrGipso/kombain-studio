"""Whisper.cpp transcription primitives shared by desktop and web frontends.

This module deliberately has no dependency on either GUI toolkit or FastAPI.
It is the canonical home for executable discovery, whisper output parsing and
the small filesystem orchestration needed by a transcription run.
"""

from __future__ import annotations

import array
import json
import math
import re
import shutil
import sys
import tempfile
import wave
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from core.models import TranscriptSegment
from core.process import run_streaming_process


LogCallback = Callable[[str], None]
CancelCheck = Callable[[], object]


def find_default_tool(name: str) -> str:
    """Find an external tool on PATH or in the supported workspace folders."""
    from_path = shutil.which(name)
    if from_path:
        return from_path
    local_candidates = (
        Path.cwd() / "tools" / "whisper.cpp" / name,
        Path.cwd() / "tools" / "ffmpeg" / name,
        Path.cwd() / name,
    )
    for candidate in local_candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def find_default_model() -> str:
    """Return the preferred locally installed ggml whisper model, if any."""
    model_dirs = (
        Path.cwd() / "tools" / "whisper.cpp" / "models",
        Path.cwd() / "models",
    )
    preferred = ("ggml-medium", "ggml-small", "ggml-base", "ggml-large", "ggml-turbo")
    for directory in model_dirs:
        if not directory.is_dir():
            continue
        models = sorted(directory.glob("*.bin"))
        for prefix in preferred:
            for model in models:
                if model.name.lower().startswith(prefix):
                    return str(model)
        if models:
            return str(models[0])
    return ""


def run_process(
    command: list[str],
    log_callback: LogCallback | None,
    *,
    cancel_check: CancelCheck | None = None,
) -> int:
    """Compatibility-sized wrapper around the shared cancellable runner."""
    return run_streaming_process(
        command,
        log_callback,
        cancel_check=cancel_check,
    )


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")


def pcm16_wav_stats(path: Path) -> tuple[float, int, float]:
    """Return ``(duration_seconds, peak, rms)`` for a PCM 16-bit WAV.

    Whisper is particularly prone to inventing short phrases for digital
    silence.  The converted WAV has a fixed, known format, so inspecting its
    samples before loading a multi-gigabyte model is both deterministic and
    much cheaper than trying to filter hallucinations afterwards.
    """
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as source:
            if (
                source.getsampwidth() != 2
                or source.getframerate() <= 0
                or source.getcomptype() != "NONE"
            ):
                raise ValueError("expected an uncompressed 16-bit PCM WAV")
            frame_count = source.getnframes()
            duration = frame_count / source.getframerate()
            peak = 0
            square_sum = 0
            sample_count = 0
            while True:
                raw = source.readframes(65_536)
                if not raw:
                    break
                samples = array.array("h")
                samples.frombytes(raw)
                if sys.byteorder != "little":
                    samples.byteswap()
                if samples:
                    peak = max(peak, max(abs(sample) for sample in samples))
                    square_sum += sum(sample * sample for sample in samples)
                    sample_count += len(samples)
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError(f"invalid WAV output: {exc}") from exc

    rms = math.sqrt(square_sum / sample_count) if sample_count else 0.0
    return duration, peak, rms


def wav_is_effectively_silent(
    stats: tuple[float, int, float],
    *,
    peak_threshold: int = 8,
    rms_threshold: float = 2.0,
) -> bool:
    """Conservatively recognize digital/near-digital silence.

    The thresholds are deliberately tiny relative to the 16-bit range.  They
    prevent known silence hallucinations without suppressing merely quiet
    speech recordings.
    """
    _duration, peak, rms = stats
    return peak <= peak_threshold and rms <= rms_threshold


def clamp_segments_to_duration(
    segments: list[TranscriptSegment],
    duration_seconds: float,
) -> list[TranscriptSegment]:
    """Drop or clamp Whisper timestamps that extend beyond the source audio."""
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        return []
    # The public project format stores whole seconds.  Rounding the upper bound
    # up preserves a genuine final partial second while still preventing the
    # common 30-second silence timestamp from leaking into a short clip.
    upper_bound = max(1, math.ceil(duration_seconds))
    result: list[TranscriptSegment] = []
    for segment in segments:
        start = _timecode_to_seconds(segment.start)
        end = _timecode_to_seconds(segment.end)
        if start < 0 or start >= upper_bound:
            continue
        end = min(upper_bound, max(start + 1, end))
        result.append(
            TranscriptSegment(
                _seconds_to_timecode(start),
                _seconds_to_timecode(end),
                segment.text,
            )
        )
    return result


def transcribe_with_whisper_cpp(
    audio_path: Path,
    whisper_cli: Path,
    model_path: Path,
    ffmpeg_path: Path,
    language: str,
    output_dir: Path,
    log_callback: LogCallback | None,
    *,
    cancel_check: CancelCheck | None = None,
) -> tuple[str, list[TranscriptSegment], Path | None, Path | None]:
    """Convert an audio file and transcribe it with whisper.cpp.

    ``cancel_check`` may either raise the caller's cancellation exception or
    return a truthy value.  The shared process runner polls it even while a
    child is silent, so cancellation is not coupled to log output.
    """
    audio_path = Path(audio_path)
    whisper_cli = Path(whisper_cli)
    model_path = Path(model_path)
    ffmpeg_path = Path(ffmpeg_path)
    output_dir = Path(output_dir)

    _require_file(audio_path, "Audio file")
    _require_file(whisper_cli, "whisper-cli executable")
    _require_file(model_path, "Whisper model")
    _require_file(ffmpeg_path, "ffmpeg executable")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_base = output_dir / "whisper_transcript"
    txt_path = output_base.with_suffix(".txt")
    json_path = output_base.with_suffix(".json")
    srt_path = output_base.with_suffix(".srt")

    # A successful child that omits one output must not make us consume an
    # artifact left behind by an older run.
    for stale_path in (txt_path, json_path, srt_path):
        stale_path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="content-pipeline-") as temp_root:
        wav_path = Path(temp_root) / "voice_16khz.wav"
        if log_callback:
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
        code = run_process(
            ffmpeg_command,
            log_callback,
            cancel_check=cancel_check,
        )
        if code != 0:
            raise RuntimeError("ffmpeg conversion failed")

        try:
            wav_stats = pcm16_wav_stats(wav_path)
        except ValueError as exc:
            raise RuntimeError(f"ffmpeg produced an unreadable WAV: {exc}") from exc
        duration_seconds, _peak, _rms = wav_stats
        if duration_seconds <= 0:
            raise ValueError("Converted audio is empty; choose a non-empty recording")
        if wav_is_effectively_silent(wav_stats):
            if log_callback:
                log_callback(
                    f"Audio is silent ({duration_seconds:.2f}s); Whisper was not started"
                )
            raise ValueError(
                "Audio contains no detectable signal; choose a recording with audible speech"
            )

        if log_callback:
            log_callback("Running whisper.cpp")
        whisper_command = [
            str(whisper_cli),
            "-m",
            str(model_path),
            "-f",
            str(wav_path),
            "-l",
            str(language or "").strip() or "auto",
            "-otxt",
            "-osrt",
            "-oj",
            "-of",
            str(output_base),
        ]
        code = run_process(
            whisper_command,
            log_callback,
            cancel_check=cancel_check,
        )
        if code != 0:
            raise RuntimeError("whisper.cpp transcription failed")

    transcript_text = (
        txt_path.read_text(encoding="utf-8-sig", errors="replace").strip()
        if txt_path.is_file()
        else ""
    )
    segments: list[TranscriptSegment] = []
    if json_path.is_file():
        try:
            segments = parse_whisper_json(json_path)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            if log_callback:
                log_callback(f"Invalid whisper JSON; using SRT fallback: {exc}")
    if not segments and srt_path.is_file():
        segments = parse_srt_segments(srt_path)
    had_timed_segments = bool(segments)
    segments = clamp_segments_to_duration(segments, duration_seconds)
    if had_timed_segments:
        # Treat timed output as authoritative.  This also removes text whose
        # only segments were outside the actual recording duration.
        transcript_text = " ".join(segment.text for segment in segments).strip()

    return (
        transcript_text,
        segments,
        json_path if json_path.is_file() else None,
        srt_path if srt_path.is_file() else None,
    )


def whisper_detect_language(path: Path) -> str:
    """Return the language decoded by whisper.cpp, or ``""`` if unavailable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
        if not isinstance(data, Mapping):
            return ""
        result = data.get("result")
        if not isinstance(result, Mapping):
            return ""
        language = result.get("language")
        return str(language).strip().lower() if language else ""
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return ""


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def parse_whisper_json(path: Path) -> list[TranscriptSegment]:
    """Parse both current and legacy whisper.cpp JSON segment layouts."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
    if not isinstance(data, Mapping):
        raise ValueError("whisper JSON root must be an object")
    raw_segments = data.get("transcription") or data.get("segments") or []
    if not isinstance(raw_segments, list):
        raise ValueError("whisper JSON segments must be a list")

    segments: list[TranscriptSegment] = []
    for item in raw_segments:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        timestamps = item.get("timestamps")
        timestamps = timestamps if isinstance(timestamps, Mapping) else {}
        start = _first_present(timestamps, "from", "start")
        if start is None:
            start = _first_present(item, "start", "t0")
        if start is None:
            start = "00:00:00"
        end = _first_present(timestamps, "to", "end")
        if end is None:
            end = _first_present(item, "end", "t1")
        if end is None:
            end = start
        segments.append(
            TranscriptSegment(normalize_timestamp(start), normalize_timestamp(end), text)
        )
    return segments


def parse_srt_segments(path: Path) -> list[TranscriptSegment]:
    """Parse SRT blocks, tolerating a BOM and subtitles without numeric IDs."""
    content = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\r?\n\s*\r?\n", content.strip())
    segments: list[TranscriptSegment] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None or timing_index + 1 >= len(lines):
            continue
        start_raw, end_raw = lines[timing_index].split("-->", 1)
        # SRT permits optional positioning settings after the end timestamp.
        end_raw = end_raw.strip().split(maxsplit=1)[0]
        text = " ".join(lines[timing_index + 1 :]).strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                normalize_timestamp(start_raw.strip()),
                normalize_timestamp(end_raw),
                text,
            )
        )
    return segments


def _seconds_to_timecode(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _timecode_to_seconds(value: str) -> int:
    parts = str(value).split(":")
    try:
        if len(parts) == 3:
            return max(0, int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
        if len(parts) == 2:
            return max(0, int(parts[0]) * 60 + int(parts[1]))
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_timestamp(value: Any) -> str:
    """Normalize whisper/SRT timestamp variants to whole-second HH:MM:SS."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _seconds_to_timecode(round(float(value)))
    text = str(value).strip()
    if "," in text:
        text = text.split(",", 1)[0]
    if "." in text:
        text = text.split(".", 1)[0]
    parts = text.split(":")
    try:
        if len(parts) == 3:
            return f"{max(0, int(parts[0])):02d}:{max(0, int(parts[1])):02d}:{max(0, int(float(parts[2]))):02d}"
        if len(parts) == 2:
            return f"00:{max(0, int(parts[0])):02d}:{max(0, int(float(parts[1]))):02d}"
        if text.isdigit():
            return _seconds_to_timecode(int(text))
    except (TypeError, ValueError, OverflowError):
        pass
    return "00:00:00"


def _configured_file(settings: Mapping[str, Any], key: str) -> Path | None:
    raw = str(settings.get(key, "") or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_file() else None


def _resolve_required_tool(
    settings: Mapping[str, Any],
    key: str,
    fallback_name: str,
) -> Path:
    configured = _configured_file(settings, key)
    if configured is not None:
        return configured
    fallback = find_default_tool(fallback_name)
    if fallback:
        return Path(fallback)
    raw = str(settings.get(key, "") or "").strip()
    return Path(raw) if raw else Path(fallback_name)


def run_transcription(
    project_dir: Path,
    audio_path: Path,
    settings: Mapping[str, Any],
    log_callback: LogCallback | None = None,
    *,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """Run transcription and return JSON-serializable project artifacts."""
    project_dir = Path(project_dir)
    audio_path = Path(audio_path)

    whisper_cli = _resolve_required_tool(settings, "whisper_cli", "whisper-cli.exe")
    ffmpeg_path = _resolve_required_tool(settings, "ffmpeg", "ffmpeg.exe")
    model_path = _configured_file(settings, "whisper_model")
    if model_path is None:
        fallback_model = find_default_model()
        raw_model = str(settings.get("whisper_model", "") or "").strip()
        model_path = Path(fallback_model or raw_model or "whisper-model.bin")

    def log(message: str) -> None:
        if log_callback:
            log_callback(message)

    transcript_text, segments, json_path, srt_path = transcribe_with_whisper_cpp(
        audio_path=audio_path,
        whisper_cli=whisper_cli,
        model_path=model_path,
        ffmpeg_path=ffmpeg_path,
        language=str(settings.get("whisper_language", "ru") or "ru"),
        output_dir=project_dir / "transcription",
        log_callback=log,
        cancel_check=cancel_check,
    )

    voice_dir = project_dir / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    voice_target = voice_dir / audio_path.name
    if audio_path.is_file() and audio_path.resolve() != voice_target.resolve():
        shutil.copy2(audio_path, voice_target)
        log(f"Voice audio saved: {voice_target}")

    detected_language = whisper_detect_language(json_path) if json_path else ""
    if detected_language:
        log(f"Определён язык аудио: {detected_language}")

    return {
        "text": transcript_text,
        "segments": [
            {"start": segment.start, "end": segment.end, "text": segment.text}
            for segment in segments
        ],
        "json_path": str(json_path) if json_path else "",
        "srt_path": str(srt_path) if srt_path else "",
        "language": detected_language,
    }
