from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from core import media_observations as observations


def _probe(*, duration: str = "4.0", width: int = 1280, height: int = 704) -> dict:
    return {
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": width,
                "height": height,
                "avg_frame_rate": "25/1",
                "nb_read_frames": "100",
            },
        ],
        "format": {"duration": duration},
    }


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_observe_image_reports_verified_dimensions(tmp_path: Path):
    path = tmp_path / "frame.png"
    Image.new("RGB", (320, 180), (12, 34, 56)).save(path)

    report = observations.observe_image(path)

    assert report["ok"] is True
    assert report["decode_ok"] is True
    assert report["exists"] is True
    assert report["nonempty"] is True
    assert (report["width"], report["height"]) == (320, 180)
    assert report["format"] == "PNG"
    assert report["issues"] == []


@pytest.mark.parametrize(
    ("create", "code"),
    [
        (lambda path: None, "missing_file"),
        (lambda path: path.write_bytes(b""), "empty_file"),
        (lambda path: path.write_bytes(b"not an image"), "image_decode_failed"),
    ],
)
def test_observe_image_reports_file_and_decode_failures(tmp_path: Path, create, code: str):
    path = tmp_path / "bad.png"
    create(path)

    report = observations.observe_image(path)

    assert report["ok"] is False
    assert code in {issue["code"] for issue in report["issues"]}


def test_evaluate_duration_uses_larger_absolute_or_relative_tolerance():
    short = observations.evaluate_duration(4.24, 4.0)
    long = observations.evaluate_duration(11.0, 10.0)
    outside = observations.evaluate_duration(11.01, 10.0)
    unchecked = observations.evaluate_duration(4.0, None)

    assert short["tolerance"] == pytest.approx(0.4)
    assert short["within_tolerance"] is True
    assert long["tolerance"] == pytest.approx(1.0)
    assert long["within_tolerance"] is True
    assert outside["within_tolerance"] is False
    assert unchecked["checked"] is False
    assert unchecked["within_tolerance"] is None


def test_evaluate_video_probe_extracts_first_video_stream_and_duration_contract():
    report = observations.evaluate_video_probe(_probe(), expected_duration=4.0)

    assert report["ok"] is True
    assert report["has_video"] is True
    assert report["duration"] == 4.0
    assert (report["width"], report["height"]) == (1280, 704)
    assert report["fps"] == 25.0
    assert report["frame_count"] == 100
    assert report["duration_check"]["within_tolerance"] is True


def test_evaluate_video_probe_reports_missing_stream_dimensions_and_duration_mismatch():
    missing = observations.evaluate_video_probe({"streams": [], "format": {"duration": "4"}})
    malformed = observations.evaluate_video_probe(
        _probe(duration="6.0", width=0, height=704),
        expected_duration=4.0,
    )

    assert {issue["code"] for issue in missing["issues"]} == {"missing_video_stream"}
    assert {issue["code"] for issue in malformed["issues"]} == {
        "invalid_dimensions",
        "duration_mismatch",
    }


def test_detector_parsers_merge_duplicates_and_close_trailing_intervals():
    stderr = """
    [blackdetect] black_start:0 black_end:1 black_duration:1
    [blackdetect] black_start:0.5 black_end:1.5 black_duration:1
    [freezedetect] lavfi.freezedetect.freeze_start: 2.0
    [freezedetect] lavfi.freezedetect.freeze_duration: 1.0
    [freezedetect] lavfi.freezedetect.freeze_end: 3.0
    [freezedetect] lavfi.freezedetect.freeze_start: 4.0
    """

    black = observations.parse_blackdetect(stderr, 5.0)
    frozen = observations.parse_freezedetect(stderr, 5.0)

    assert black == [{"start": 0.0, "end": 1.5, "duration": 1.5}]
    assert frozen == [
        {"start": 2.0, "end": 3.0, "duration": 1.0},
        {"start": 4.0, "end": 5.0, "duration": 1.0},
    ]
    assert observations.interval_fraction(black, 5.0) == pytest.approx(0.3)
    assert observations.interval_fraction(frozen, 5.0) == pytest.approx(0.4)


def test_blackdetect_trailing_interval_is_closed_at_media_duration():
    black = observations.parse_blackdetect("black_start:3.25", 5.0)

    assert black == [{"start": 3.25, "end": 5.0, "duration": 1.75}]
    assert observations.interval_fraction(black, 5.0) == pytest.approx(0.35)


def test_normalize_intervals_clamps_to_media_duration_and_avoids_double_counting():
    intervals = [(-1.0, 2.0), (1.5, 4.0), (4.0, 8.0), (9.0, 9.0)]

    merged = observations.normalize_intervals(intervals, 5.0)

    assert merged == [{"start": 0.0, "end": 5.0, "duration": 5.0}]
    assert observations.interval_fraction(merged, 5.0) == 1.0


def test_evaluate_decode_result_is_pure_and_preserves_diagnostic_tail():
    good = observations.evaluate_decode_result(0, "")
    failed = observations.evaluate_decode_result(1, "corrupt packet")
    timed_out = observations.evaluate_decode_result(None, timed_out=True)

    assert good["ok"] is True
    assert failed["ok"] is False
    assert failed["issues"][0]["code"] == "decode_failed"
    assert failed["issues"][0]["details"]["stderr"] == "corrupt packet"
    assert timed_out["issues"][0]["code"] == "decode_timeout"


def test_observe_video_combines_probe_decode_and_detector_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video")
    detector_log = " ".join(
        [
            "black_start:0 black_end:0.5 black_duration:0.5",
            "freeze_start:1 freeze_end:2 freeze_duration:1",
            "freeze_start:3",
        ]
    )
    completed = iter(
        [
            _completed(stdout=json.dumps(_probe())),
            _completed(),
            _completed(stderr=detector_log),
        ]
    )
    monkeypatch.setattr(observations, "_resolve_ffprobe", lambda _ffmpeg: "ffprobe")
    monkeypatch.setattr(observations, "_resolve_ffmpeg", lambda _ffmpeg: "ffmpeg")
    monkeypatch.setattr(observations.subprocess, "run", lambda *_args, **_kwargs: next(completed))

    report = observations.observe_video(path, "ffmpeg", expected_duration=4.0)

    assert report["ok"] is True
    assert report["probe_ok"] is True
    assert report["decode_ok"] is True
    assert report["detector_ok"] is True
    assert report["duration"] == 4.0
    assert report["black_fraction"] == pytest.approx(0.125)
    assert report["frozen_fraction"] == pytest.approx(0.5)
    assert report["freeze_intervals"][-1] == {
        "start": 3.0,
        "end": 4.0,
        "duration": 1.0,
    }


def test_observe_video_short_circuits_missing_file_without_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        observations.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("missing files must not launch tools"),
    )

    report = observations.observe_video(tmp_path / "missing.mp4", "ffmpeg", 4.0)

    assert report["ok"] is False
    assert report["duration"] == 0.0
    assert report["black_fraction"] == 0.0
    assert report["frozen_fraction"] == 0.0
    assert {issue["code"] for issue in report["issues"]} == {"missing_file"}


def test_observe_video_surfaces_decode_and_detector_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video")
    completed = iter(
        [
            _completed(stdout=json.dumps(_probe())),
            _completed(stderr="invalid packet", returncode=1),
            _completed(stderr="filter unavailable", returncode=1),
        ]
    )
    monkeypatch.setattr(observations, "_resolve_ffprobe", lambda _ffmpeg: "ffprobe")
    monkeypatch.setattr(observations, "_resolve_ffmpeg", lambda _ffmpeg: "ffmpeg")
    monkeypatch.setattr(observations.subprocess, "run", lambda *_args, **_kwargs: next(completed))

    report = observations.observe_video(path, "ffmpeg")

    assert report["ok"] is False
    assert report["probe_ok"] is True
    assert report["decode_ok"] is False
    assert report["detector_ok"] is False
    assert {issue["code"] for issue in report["issues"]} == {
        "decode_failed",
        "detector_failed",
    }
