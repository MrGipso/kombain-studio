from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.main import app
from backend.routers import export_router
from core import export
from core.export_profiles import (
    DEFAULT_EXPORT_PROFILE,
    EXPORT_PROFILES,
    export_profile_catalog,
    resolve_export_profile,
)
from core.media_qc import evaluate_probe


def _probe(*, width: int, height: int, fps: str = "25/1") -> dict:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": width,
                "height": height,
                "avg_frame_rate": fps,
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
            },
        ],
        "format": {"duration": "12.5"},
    }


def test_delivery_profile_preserves_previous_default_contract():
    profile = resolve_export_profile()

    assert DEFAULT_EXPORT_PROFILE == "delivery"
    assert (profile.width, profile.height, profile.fps) == (1920, 1080, 25)
    assert profile.video_codec == "h264"
    assert profile.crf == 18
    assert profile.audio_codec == "aac"
    assert profile.audio_bitrate_kbps == 192
    assert export.video_encode_args("ffmpeg", "off", profile) == [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
    ]


def test_profiles_validate_every_ffmpeg_value_and_reject_flag_injection():
    delivery = resolve_export_profile("delivery")

    with pytest.raises(ValueError, match="dimensions must be even"):
        replace(delivery, width=1919)
    with pytest.raises(ValueError, match="unsupported video codec"):
        replace(delivery, video_codec="h264 -vf evil")
    with pytest.raises(ValueError, match="audio_bitrate_kbps"):
        replace(delivery, audio_bitrate_kbps=10_000)
    with pytest.raises(ValueError, match="audio_channels"):
        replace(delivery, audio_channels=6)
    with pytest.raises(ValueError, match="below the target"):
        replace(delivery, video_bitrate_kbps=5_000, max_video_bitrate_kbps=4_000)
    with pytest.raises(ValueError, match="unknown export profile"):
        resolve_export_profile("delivery;rm -rf output")


def test_preview_encoder_args_are_bounded_profile_tokens():
    args = export.video_encode_args("ffmpeg", "off", "preview")

    assert args == [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-b:v", "3500k", "-maxrate", "5000k", "-bufsize", "10000k",
        "-pix_fmt", "yuv420p",
    ]
    assert all(";" not in token and "\n" not in token for token in args)


def test_explicit_delivery_profile_normalizes_even_without_voice():
    assert export._normalize_without_voice(False, profile_was_explicit=True) is True
    assert export._normalize_without_voice(False, profile_was_explicit=False) is False
    assert export._normalize_without_voice(True, profile_was_explicit=False) is True


def test_media_qc_uses_selected_profile_contract():
    preview_probe = _probe(width=1280, height=720)

    preview = evaluate_probe(preview_probe, profile="preview")
    delivery = evaluate_probe(preview_probe, profile="delivery")

    assert preview["ok"] is True
    assert preview["profile"] == "preview"
    assert preview["expected"]["width"] == 1280
    assert delivery["ok"] is False
    assert {issue["code"] for issue in delivery["issues"]} == {"bad_resolution"}


def test_explicit_profile_makes_codec_mismatch_a_qc_error():
    probe = _probe(width=1920, height=1080)
    probe["streams"][0]["codec_name"] = "hevc"

    report = evaluate_probe(probe, profile="master")

    assert report["ok"] is False
    assert report["issues"][0]["code"] == "video_codec"
    assert report["issues"][0]["level"] == "error"


def test_master_qc_checks_reported_bitrates_and_audio_layout():
    probe = _probe(width=1920, height=1080)
    probe["streams"][0]["bit_rate"] = "40000000"
    probe["streams"][1].update({"bit_rate": "96000", "channels": 1})

    report = evaluate_probe(probe, profile="master")

    assert report["ok"] is False
    assert {issue["code"] for issue in report["issues"]} == {
        "video_bitrate", "audio_bitrate", "audio_channels",
    }


def test_catalog_and_openapi_advertise_closed_profile_enum():
    catalog = export_profile_catalog()
    assert catalog["default"] == "delivery"
    assert [item["name"] for item in catalog["profiles"]] == list(EXPORT_PROFILES)

    operation = app.openapi()["paths"]["/api/projects/{project_id}/export"]["post"]
    parameter = next(item for item in operation["parameters"] if item["name"] == "profile")
    assert parameter["schema"]["enum"] == ["preview", "delivery", "master"]
    assert parameter["schema"]["default"] == "delivery"


def test_project_profile_catalog_endpoint_returns_server_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(export_router, "load_settings", lambda: SimpleNamespace(output_base=""))
    monkeypatch.setattr(export_router, "safe_project_path", lambda *_args: tmp_path)

    payload = asyncio.run(export_router.get_export_profiles("project"))

    assert payload["default"] == "delivery"
    assert len(payload["profiles"]) == 3


def test_master_rejects_preview_profile():
    with pytest.raises(HTTPException) as caught:
        export_router._validate_profile_for_mode("master", "preview")
    assert caught.value.status_code == 422

    export_router._validate_profile_for_mode("master", "master")
    export_router._validate_profile_for_mode("master", "delivery")
