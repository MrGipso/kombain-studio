from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from core.models import TranscriptSegment
from plugins.analyzers.lm_studio import LmStudioAnalyzer
from plugins.generators.comfy import ComfyGenerator
from plugins.transcribers.whisper_cpp import WhisperCppTranscriber
from plugins.analyzers import lm_studio as analyzer_module
from plugins.generators import comfy as generator_module
from plugins.transcribers import whisper_cpp as transcriber_module


def test_plugins_import_without_desktop_monolith_or_tkinter():
    root = Path(__file__).resolve().parents[1]
    script = r"""
import builtins
import sys
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "content_pipeline_gui" or name.startswith("tkinter"):
        raise AssertionError(f"plugin imported desktop dependency: {name}")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import plugins.analyzers.lm_studio
import plugins.generators.comfy
import plugins.transcribers.whisper_cpp
assert "content_pipeline_gui" not in sys.modules
assert "tkinter" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_transcriber_plugin_delegates_to_canonical_core(monkeypatch, tmp_path: Path):
    audio = tmp_path / "voice.wav"
    cli = tmp_path / "whisper-cli.exe"
    model = tmp_path / "model.bin"
    ffmpeg = tmp_path / "ffmpeg.exe"
    for path in (audio, cli, model, ffmpeg):
        path.write_bytes(b"x")
    raw_json = tmp_path / "result.json"
    raw_json.write_text(json.dumps({"result": {"language": "ru"}}), encoding="utf-8")
    srt = tmp_path / "result.srt"
    srt.write_text("", encoding="utf-8")
    written = {}
    project = SimpleNamespace(
        dir=tmp_path / "project",
        settings=SimpleNamespace(
            whisper_cli=str(cli),
            whisper_model=str(model),
            ffmpeg=str(ffmpeg),
        ),
        write_text=lambda key, value: written.__setitem__(key, value),
        write_json=lambda key, value: written.__setitem__(key, value),
    )
    seen = {}

    def transcribe(**kwargs):
        seen.update(kwargs)
        return (
            "spoken words",
            [TranscriptSegment("00:00:00", "00:00:02", "spoken words")],
            raw_json,
            srt,
        )

    monkeypatch.setattr(
        transcriber_module.transcription,
        "transcribe_with_whisper_cpp",
        transcribe,
    )

    result = WhisperCppTranscriber().transcribe(project, audio, language="auto")

    assert result.success is True
    assert result.text == "spoken words"
    assert seen["language"] == "auto"
    assert written["transcript"] == "spoken words"
    assert written["transcript_segments"][0]["end"] == "00:00:02"
    assert written["whisper_raw"]["result"]["language"] == "ru"


def test_comfy_plugin_chains_generated_image_into_video(monkeypatch, tmp_path: Path):
    settings = SimpleNamespace()
    project = SimpleNamespace(dir=tmp_path, settings=settings)
    card = SimpleNamespace(
        image_model="krea2_turbo",
        video_model="ltx2.3_i2v",
        image_output="",
        video_output="",
    )
    image = tmp_path / "image.png"
    video = tmp_path / "video.mp4"

    def generate_image(_url, _dir, _card, **kwargs):
        assert kwargs["settings"] is settings
        image.write_bytes(b"image")
        return image

    def generate_video(_url, _dir, current_card, **kwargs):
        assert current_card.image_output == str(image)
        assert kwargs["settings"] is settings
        video.write_bytes(b"video")
        return video

    monkeypatch.setattr(generator_module.comfy_generation, "generate_comfy_image", generate_image)
    monkeypatch.setattr(
        generator_module.comfy_generation,
        "generate_comfy_ltx_video",
        generate_video,
    )

    result = ComfyGenerator().generate(project, card)

    assert result.success is True
    assert result.outputs == {"image": str(image), "video": str(video)}
    assert card.video_output == str(video)


def test_analyzer_passes_reference_policy_from_snapshot(monkeypatch, tmp_path: Path):
    settings = SimpleNamespace(
        lm_auto_unload=False,
        reference_enable=False,
        reference_density="normal",
    )
    written = {}
    project = SimpleNamespace(
        dir=tmp_path,
        settings=settings,
        metadata={},
        read_json=lambda _key: [],
        write_json=lambda key, value: written.__setitem__(key, value),
    )
    captured = {}

    def plan(*_args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(analyzer_module.shot_planner, "call_lm_studio_for_shots", plan)
    monkeypatch.setattr(analyzer_module.board_store, "build_asset_board", lambda _dir: [])
    monkeypatch.setattr(analyzer_module.board_store, "write_asset_board", lambda *_args: None)

    result = LmStudioAnalyzer().analyze_shots(project, [])

    assert result.success is True
    assert captured["reference_density"] == "off"
    assert written["shot_list"] == []
