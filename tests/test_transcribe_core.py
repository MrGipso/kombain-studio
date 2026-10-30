from __future__ import annotations

import json
import math
import threading
import wave
from pathlib import Path

import pytest

from core import process, transcribe


def _write_pcm16_wav(path: Path, samples: list[int], sample_rate: int = 16_000) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(b"".join(int(sample).to_bytes(2, "little", signed=True) for sample in samples))


def test_parse_whisper_json_supports_current_and_legacy_layouts(tmp_path: Path):
    payload = {
        "transcription": [
            {
                "timestamps": {"from": "00:00:01,250", "to": "00:00:03,900"},
                "text": " Current segment ",
            },
            {"start": 4.2, "end": 7.6, "text": "Legacy segment"},
            {"start": 8, "end": 9, "text": "   "},
            "not-an-object",
        ]
    }
    path = tmp_path / "whisper.json"
    path.write_text("\ufeff" + json.dumps(payload), encoding="utf-8")

    segments = transcribe.parse_whisper_json(path)

    assert [(item.start, item.end, item.text) for item in segments] == [
        ("00:00:01", "00:00:03", "Current segment"),
        ("00:00:04", "00:00:08", "Legacy segment"),
    ]


def test_parse_srt_accepts_missing_ids_multiline_text_and_positioning(tmp_path: Path):
    path = tmp_path / "whisper.srt"
    path.write_bytes((
        "\ufeff1\r\n"
        "00:00:00,100 --> 00:00:02,900\r\n"
        "First line\r\nSecond line\r\n\r\n"
        "00:01:03.000 --> 00:01:05.250 align:start position:0%\r\n"
        "No numeric id\r\n\r\n"
        "broken block\r\n"
    ).encode("utf-8"))

    segments = transcribe.parse_srt_segments(path)

    assert [(item.start, item.end, item.text) for item in segments] == [
        ("00:00:00", "00:00:02", "First line Second line"),
        ("00:01:03", "00:01:05", "No numeric id"),
    ]


def test_transcription_discards_stale_outputs_and_falls_back_to_srt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    audio = tmp_path / "voice.mp3"
    whisper_cli = tmp_path / "whisper-cli.exe"
    model = tmp_path / "model.bin"
    ffmpeg = tmp_path / "ffmpeg.exe"
    for path in (audio, whisper_cli, model, ffmpeg):
        path.write_bytes(b"placeholder")

    output_dir = tmp_path / "transcription"
    output_dir.mkdir()
    (output_dir / "whisper_transcript.txt").write_text("stale transcript", encoding="utf-8")
    (output_dir / "whisper_transcript.json").write_text(
        json.dumps({"transcription": [{"start": 99, "end": 100, "text": "stale"}]}),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command, _log, *, cancel_check=None):
        calls.append(command)
        if cancel_check:
            cancel_check()
        if "-ar" in command:
            _write_pcm16_wav(Path(command[-1]), [0, 1200, -1200, 500] * 16_000)
        if "-of" in command:
            base = Path(command[command.index("-of") + 1])
            base.with_suffix(".json").write_text("{invalid", encoding="utf-8")
            base.with_suffix(".srt").write_text(
                "1\n00:00:01,000 --> 00:00:03,000\nFresh result\n",
                encoding="utf-8",
            )
        return 0

    monkeypatch.setattr(transcribe, "run_process", fake_run)
    logs: list[str] = []
    checks = {"count": 0}

    def check_cancel():
        checks["count"] += 1

    text, segments, json_path, srt_path = transcribe.transcribe_with_whisper_cpp(
        audio,
        whisper_cli,
        model,
        ffmpeg,
        "",
        output_dir,
        logs.append,
        cancel_check=check_cancel,
    )

    assert text == "Fresh result"
    assert [(item.start, item.end, item.text) for item in segments] == [
        ("00:00:01", "00:00:03", "Fresh result")
    ]
    assert json_path == output_dir / "whisper_transcript.json"
    assert srt_path == output_dir / "whisper_transcript.srt"
    assert "stale transcript" not in text
    assert calls[1][calls[1].index("-l") + 1] == "auto"
    assert checks["count"] == 2
    assert any("SRT fallback" in message for message in logs)


def test_pcm_stats_detect_silence_and_measure_signal(tmp_path: Path):
    silence = tmp_path / "silence.wav"
    signal = tmp_path / "signal.wav"
    _write_pcm16_wav(silence, [0] * 16_000)
    _write_pcm16_wav(signal, [0, 1000, -1000, 0] * 4000)

    silence_stats = transcribe.pcm16_wav_stats(silence)
    signal_stats = transcribe.pcm16_wav_stats(signal)

    assert silence_stats == (1.0, 0, 0.0)
    assert transcribe.wav_is_effectively_silent(silence_stats) is True
    assert signal_stats[0] == 1.0
    assert signal_stats[1] == 1000
    assert math.isclose(signal_stats[2], math.sqrt(500_000), rel_tol=1e-6)
    assert transcribe.wav_is_effectively_silent(signal_stats) is False


def test_silent_audio_is_rejected_before_whisper_starts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    audio = tmp_path / "voice.wav"
    whisper_cli = tmp_path / "whisper-cli.exe"
    model = tmp_path / "model.bin"
    ffmpeg = tmp_path / "ffmpeg.exe"
    for path in (audio, whisper_cli, model, ffmpeg):
        path.write_bytes(b"placeholder")
    calls: list[list[str]] = []

    def fake_run(command, _log, *, cancel_check=None):
        calls.append(command)
        if "-ar" in command:
            _write_pcm16_wav(Path(command[-1]), [0] * 12_800)
            return 0
        raise AssertionError("whisper must not run for silent audio")

    monkeypatch.setattr(transcribe, "run_process", fake_run)
    logs: list[str] = []

    with pytest.raises(ValueError, match="no detectable signal"):
        transcribe.transcribe_with_whisper_cpp(
            audio,
            whisper_cli,
            model,
            ffmpeg,
            "auto",
            tmp_path / "out",
            logs.append,
        )

    assert len(calls) == 1
    assert any("Whisper was not started" in message for message in logs)


def test_segments_are_clamped_to_real_audio_duration():
    segments = [
        transcribe.TranscriptSegment("00:00:00", "00:00:29", "kept and clamped"),
        transcribe.TranscriptSegment("00:00:02", "00:00:04", "outside"),
    ]

    clamped = transcribe.clamp_segments_to_duration(segments, 0.8)

    assert [(item.start, item.end, item.text) for item in clamped] == [
        ("00:00:00", "00:00:01", "kept and clamped")
    ]


class _Cancelled(Exception):
    pass


class _SilentStdout:
    def __init__(self, owner: "_SilentProcess"):
        self.owner = owner
        self.closed = False

    def __iter__(self):
        self.owner.release.wait(timeout=2)
        return
        yield ""  # pragma: no cover - keeps this a generator

    def close(self):
        self.closed = True
        self.owner.release.set()


class _SilentProcess:
    def __init__(self):
        self.release = threading.Event()
        self.stdout = _SilentStdout(self)
        self.terminated = False
        self.killed = False

    def poll(self):
        return -15 if self.terminated or self.killed else None

    def terminate(self):
        self.terminated = True
        self.release.set()

    def kill(self):
        self.killed = True
        self.release.set()

    def wait(self, timeout=None):
        return -15 if self.terminated or self.killed else 0


def test_cancel_check_terminates_child_even_when_it_emits_no_output(
    monkeypatch: pytest.MonkeyPatch,
):
    child = _SilentProcess()
    monkeypatch.setattr(process.subprocess, "Popen", lambda *args, **kwargs: child)
    checks = {"count": 0}

    def cancel_after_poll():
        checks["count"] += 1
        if checks["count"] >= 2:
            raise _Cancelled("stop")

    with pytest.raises(_Cancelled, match="stop"):
        process.run_streaming_process(
            ["silent-tool"],
            cancel_check=cancel_after_poll,
            cancel_poll_seconds=0.01,
        )

    assert child.terminated is True
    assert child.stdout.closed is True

