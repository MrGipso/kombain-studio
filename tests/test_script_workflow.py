from __future__ import annotations

import wave
from pathlib import Path

import pytest

from core.omnivoice_tts import BUILTIN_VOICES, assemble_wav, compile_omnivoice_text, discover_voice_profiles, wav_duration
from core.script_workflow import (
    apply_requested_duration,
    assess_script_quality,
    build_voice_plan,
    clean_subtitle_text,
    default_script,
    estimate_duration_sec,
    migrate_script,
    split_into_beats,
)
from core.youtube_subtitles import validate_youtube_url


def test_caption_cleanup_removes_timing_and_duplicate_lines():
    source = """WEBVTT

00:00:00.000 --> 00:00:02.000
Первая строка

00:00:02.000 --> 00:00:04.000
Первая строка
Вторая строка
"""
    assert clean_subtitle_text(source) == "Первая строка\n\nВторая строка"


def test_caption_cleanup_removes_vtt_metadata_and_audio_cues():
    source = """WEBVTT
Kind: captions
Language: ru

00:00:00.000 --> 00:00:02.000
>> [музыка] Полезный текст
"""
    assert clean_subtitle_text(source) == "Полезный текст"


def test_rewrite_quality_blocks_severely_short_script():
    script = migrate_script({
        "mode": "rewrite",
        "target_duration_sec": 600,
        "estimated_wpm": 135,
        "final_text": "слишком коротко",
        "beats": [{"text": "слишком коротко"}],
        "quality": {"target_words": 1215, "total_beats": 1, "completed_beats": 1},
    })

    quality = assess_script_quality(script)
    assert quality["status"] == "incomplete"
    assert quality["actual_words"] == 2


def test_rewrite_quality_uses_strict_ready_and_editor_review_bands():
    base = {
        "mode": "rewrite",
        "target_duration_sec": 600,
        "estimated_wpm": 135,
        "beats": [{"text": "текст"}],
        "quality": {"target_words": 100, "total_beats": 1, "completed_beats": 1},
    }
    review = assess_script_quality({**base, "final_text": " ".join(["слово"] * 90)})
    ready = assess_script_quality({**base, "final_text": " ".join(["слово"] * 98)})

    assert review["status"] == "review"
    assert ready["status"] == "ready"


def test_ready_duration_is_automatic_and_voice_plan_is_provider_neutral():
    text = "Первое предложение. Второе предложение!\n\nНовый абзац."
    duration = estimate_duration_sec(text, 120)
    plan = build_voice_plan(text)
    assert duration > 2
    assert len(plan["chunks"]) == 3
    assert plan["chunks"][-2]["pause_after_ms"] >= 750
    assert all(chunk["provider_tags"] == [] for chunk in plan["chunks"])


def test_voice_plan_applies_speed_and_pause_scale():
    plan = build_voice_plan("Первое предложение.\n\nВторое предложение!", speed=1.15, pause_scale=1.5)

    assert plan["settings"] == {"speed": 1.15, "pause_scale": 1.5}
    assert plan["chunks"][0]["speed"] == 1.15
    assert plan["chunks"][1]["speed"] > 1.15
    assert plan["chunks"][0]["pause_after_ms"] >= 1125


def test_voice_plan_directs_emotion_without_inventing_provider_tags():
    plan = build_voice_plan("Почему это важно? Возникла критическая опасность. Итак, главный вывод ясен!")

    assert [chunk["emotion"] for chunk in plan["chunks"]] == ["curiosity", "tension", "confident"]
    assert all(chunk["provider_tags"] == [] for chunk in plan["chunks"])
    assert plan["chunks"][1]["speed"] < 1.0


def test_omnivoice_tags_are_allowlisted_and_not_duplicated():
    assert compile_omnivoice_text("Текст", ["[sigh]", "[angry]"]) == "[sigh] Текст"
    assert compile_omnivoice_text("[laughter] Текст", ["[laughter]"]) == "[laughter] Текст"
    assert compile_omnivoice_text("[happy] Текст", []) == "Текст"


def test_legacy_script_migrates_without_losing_beat_text():
    migrated = migrate_script({
        "topic": "Legacy",
        "hooks": ["Хук"],
        "hook_selected": 0,
        "beats": [{"title": "Бит", "text": "Основной текст", "duration_sec": 30}],
    })
    assert migrated["schema_version"] == 2
    assert migrated["final_text"] == "Хук\n\nОсновной текст"
    assert migrated["beats"][0]["id"]


def test_rewrite_beats_can_be_scaled_to_target():
    beats = split_into_beats("Первый блок.\n\nВторой блок.", target_seconds=600)
    assert len(beats) == 2
    assert sum(beat["duration_sec"] for beat in beats) == 600


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "https://example.com/watch?v=x",
    "https://user:secret@youtube.com/watch?v=x",
])
def test_youtube_url_rejects_untrusted_sources(url: str):
    with pytest.raises(ValueError):
        validate_youtube_url(url)


def _write_silence(path: Path, frames: int = 2400):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\x00" * frames * 2)


def test_wav_assembly_inserts_planned_pause(tmp_path: Path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    output = tmp_path / "narration.wav"
    _write_silence(first)
    _write_silence(second)
    manifest = assemble_wav([(first, 500), (second, 0)], output)
    assert wav_duration(output) == pytest.approx(0.7, abs=0.01)
    assert manifest[0]["pause_after_ms"] == 500


def test_new_project_has_ready_omnivoice_profiles(tmp_path: Path):
    profiles = discover_voice_profiles(tmp_path / "project", tmp_path / "bundled")
    assert {profile["id"] for profile in profiles} >= {
        "studio_documentary_m", "studio_explainer_f", "studio_dynamic_m",
    }
    assert all(profile["type"] == "preset" for profile in profiles)
    explainer = next(profile for profile in BUILTIN_VOICES if profile["id"] == "studio_explainer_f")
    assert explainer["instruct"] == "female, young adult, moderate pitch"


def _rewrite_script() -> dict:
    script = default_script()
    script.update({
        "mode": "rewrite",
        "source_text": "Проверенный исходный материал для нового сценария.",
        "target_duration_sec": 600,
        "estimated_wpm": 135,
    })
    return script


def test_process_request_updates_duration_selected_after_import():
    script = _rewrite_script()

    result = apply_requested_duration(script, 23)

    assert result["target_duration_sec"] == 23 * 60


def test_process_request_uses_source_duration_with_small_automatic_buffer():
    script = _rewrite_script()
    script["source_estimated_duration_sec"] = 25 * 60
    script["duration_mode"] = "manual"

    result = apply_requested_duration(script, None)

    assert result["duration_mode"] == "auto"
    assert result["target_duration_min_sec"] == round(25 * 60 * 0.95)
    assert result["target_duration_sec"] == 25 * 60 + 90
    assert result["target_duration_max_sec"] == 27 * 60


@pytest.mark.parametrize("minutes", [0, 181])
def test_process_request_rejects_invalid_duration(minutes):
    with pytest.raises(ValueError, match="от 1 до 180"):
        apply_requested_duration(_rewrite_script(), minutes)
