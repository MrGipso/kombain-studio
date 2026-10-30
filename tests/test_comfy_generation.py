from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import comfy
from core import comfy_generation as generation
from core.config import Settings


def _write_workflow(path: Path, workflow: dict) -> Path:
    path.write_text(json.dumps(workflow), encoding="utf-8")
    return path


def test_image_workflow_uses_the_supplied_settings_snapshot(monkeypatch, tmp_path: Path):
    workflow_path = _write_workflow(
        tmp_path / "krea.json",
        {
            "30:19": {"inputs": {"value": "old"}},
            "29": {"inputs": {"filename_prefix": "old"}},
            "sampler": {"inputs": {"seed": 1}},
        },
    )
    settings = Settings(comfy_krea_workflow=str(workflow_path))
    monkeypatch.setattr(
        generation,
        "load_settings",
        lambda: pytest.fail("explicit settings must prevent a global settings reload"),
    )

    workflow = generation.prepare_image_workflow(
        "krea2_turbo",
        "documentary frame",
        7,
        settings=settings,
    )

    assert workflow["30:19"]["inputs"]["value"] == "documentary frame"
    assert workflow["29"]["inputs"]["filename_prefix"].endswith("shot_007_krea2")
    assert workflow["sampler"]["inputs"]["seed"] != 1


def test_ltx_workflow_applies_custom_path_resolution_duration_and_negative(tmp_path: Path):
    workflow_path = _write_workflow(
        tmp_path / "ltx.json",
        {
            "269": {"inputs": {"image": "old.png"}},
            "267:266": {"inputs": {"value": "old prompt"}},
            "273": {"inputs": {"filename_prefix": "old"}},
            "267:290": {"inputs": {"value": 0}},
            "267:257": {"inputs": {"value": 0}},
            "267:258": {"inputs": {"value": 0}},
            "267:247": {"inputs": {"text": "base negative"}},
        },
    )
    settings = Settings(
        comfy_ltx_workflow=str(workflow_path),
        ltx_resolution="1280x704",
    )

    workflow = generation.prepare_ltx_workflow(
        "slow camera move",
        "input.png",
        4,
        duration_seconds=999,
        negative_extra="no neon",
        settings=settings,
    )

    assert workflow["269"]["inputs"]["image"] == "input.png"
    assert workflow["267:266"]["inputs"]["value"] == "slow camera move"
    assert workflow["267:290"]["inputs"]["value"] == generation.MAX_LTX_DURATION
    assert workflow["267:257"]["inputs"]["value"] == 1280
    assert workflow["267:258"]["inputs"]["value"] == 704
    assert workflow["267:247"]["inputs"]["text"] == "base negative, no neon"


@pytest.mark.parametrize(
    ("mode", "priority", "expected"),
    [
        ("off", "low", False),
        ("all", "high", True),
        ("auto", "medium", True),
        ("auto", "high", False),
    ],
)
def test_ltx_draft_policy_uses_job_snapshot(mode: str, priority: str, expected: bool):
    card = SimpleNamespace(priority=priority)
    settings = Settings(ltx_draft_mode=mode)

    assert generation.ltx_draft_for_card(card, settings) is expected


def test_music_workflow_override_is_used_by_generation_and_diagnostics(tmp_path: Path):
    workflow_path = _write_workflow(
        tmp_path / "ace.json",
        {
            "94": {"inputs": {"tags": "", "lyrics": "", "bpm": 90, "duration": 30}},
            "98": {"inputs": {"seconds": 30}},
            "107": {"inputs": {"filename_prefix": "old"}},
            "3": {"inputs": {"steps": 30, "seed": 1}},
        },
    )
    settings = Settings(comfy_ace_workflow=str(workflow_path))

    workflow = generation.prepare_music_workflow(
        "cinematic ambient",
        84,
        999,
        settings=settings,
    )

    assert workflow["94"]["inputs"]["tags"] == "cinematic ambient"
    assert workflow["94"]["inputs"]["lyrics"] == "[instrumental]"
    assert workflow["94"]["inputs"]["bpm"] == 84
    assert workflow["94"]["inputs"]["duration"] == generation.MAX_ACE_DURATION
    assert workflow["98"]["inputs"]["seconds"] == generation.MAX_ACE_DURATION
    assert workflow["3"]["inputs"]["steps"] == 8
    assert comfy.configured_workflow_paths(settings)["music"] == workflow_path.resolve()


def test_music_outputs_never_collide_within_the_same_second(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(generation, "prepare_music_workflow", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(generation, "comfy_queue_prompt", lambda *_args: "prompt")
    monkeypatch.setattr(
        generation,
        "comfy_wait_for_history",
        lambda *_args, **_kwargs: {
            "outputs": {"1": {"audio": [{"filename": "result.mp3"}]}}
        },
    )

    def download(_url, _item, target):
        target.write_bytes(b"audio")

    monkeypatch.setattr(generation, "comfy_download_file", download)
    monkeypatch.setattr(generation, "_limit_music_peak", lambda *_args: None)
    monkeypatch.setattr(generation.time, "strftime", lambda *_args: "20260712_120000")

    first = generation.generate_comfy_music("http://comfy", tmp_path, "ambient")
    second = generation.generate_comfy_music("http://comfy", tmp_path, "ambient")

    assert first != second
    assert first.is_file() and second.is_file()
    assert first.name.startswith("track_20260712_120000_")


def test_web_generation_routers_import_without_tkinter():
    root = Path(__file__).resolve().parents[1]
    script = """
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith('tkinter'):
        raise AssertionError(f'headless backend imported {name}')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import backend.routers.assets
import backend.routers.music
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
