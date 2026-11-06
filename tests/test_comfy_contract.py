"""Marker contract between the engine and replaceable ComfyUI workflows."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from backend.routers import assets as assets_router
from backend.routers import diagnostics
from core import comfy
from core import comfy_contract as contract
from core import comfy_generation as generation
from core.config import Settings


FROZEN_TIME = 1_700_000_000.123


@pytest.fixture
def frozen_time(monkeypatch):
    monkeypatch.setattr(generation.time, "time", lambda: FROZEN_TIME)


def _write(path: Path, workflow: dict) -> Path:
    path.write_text(json.dumps(workflow), encoding="utf-8")
    return path


def _bundled(name: str) -> dict:
    return json.loads(comfy.DEFAULT_WORKFLOW_PATHS[name].read_text(encoding="utf-8"))


def _without_titles(workflow: dict) -> dict:
    clean = copy.deepcopy(workflow)
    for node in clean.values():
        node.pop("_meta", None)
    return clean


def _prepare(kind: str, settings: Settings) -> dict:
    if kind == "krea":
        return generation.prepare_image_workflow("krea2_turbo", "harbour at dawn", 7, settings=settings)
    if kind == "ideogram":
        return generation.prepare_image_workflow("ideogram4", "poster", 8, settings=settings)
    if kind in {"ltx", "ltx_draft"}:
        return generation.prepare_ltx_workflow(
            "slow dolly",
            "shot_input.png",
            3,
            duration_seconds=7.4,
            draft=kind == "ltx_draft",
            negative_extra="no neon",
            settings=settings,
        )
    return generation.prepare_music_workflow("ambient pads", 84, 61, settings=settings)


_SETTING_KEYS = {
    "krea": "comfy_krea_workflow",
    "ideogram": "comfy_ideogram_workflow",
    "ltx": "comfy_ltx_workflow",
    "ltx_draft": "comfy_ltx_draft_workflow",
    "music": "comfy_ace_workflow",
}


@pytest.mark.parametrize("name", sorted(comfy.DEFAULT_WORKFLOW_PATHS))
def test_bundled_workflows_resolve_by_markers_without_warnings(name: str):
    report = contract.contract_report(_bundled(name), name)

    assert report["ok"] is True
    assert report["mode"] == contract.MODE_MARKERS
    assert report["warnings"] == []


def test_bundled_payloads_write_the_historical_node_ids(frozen_time):
    settings = Settings(ltx_resolution="1280x704")

    krea = _prepare("krea", settings)
    ideogram = _prepare("ideogram", settings)
    ltx = _prepare("ltx", settings)
    music = _prepare("music", settings)

    assert krea["30:19"]["inputs"]["value"] == "harbour at dawn"
    assert krea["29"]["inputs"]["filename_prefix"] == "content_pipeline/shot_007_krea2"
    assert ideogram["98:24"]["inputs"]["text"] == "poster"
    assert ideogram["158"]["inputs"]["filename_prefix"] == "content_pipeline/shot_008_ideogram4"
    assert ltx["269"]["inputs"]["image"] == "shot_input.png"
    assert ltx["267:266"]["inputs"]["value"] == "slow dolly"
    assert ltx["273"]["inputs"]["filename_prefix"] == "content_pipeline/video/shot_003_ltx2_3"
    assert ltx["267:290"]["inputs"]["value"] == 7.4
    assert (ltx["267:257"]["inputs"]["value"], ltx["267:258"]["inputs"]["value"]) == (1280, 704)
    assert ltx["267:247"]["inputs"]["text"].endswith(", no neon")
    text = music["94"]["inputs"]
    assert (text["tags"], text["lyrics"], text["bpm"], text["duration"]) == (
        "ambient pads",
        "[instrumental]",
        84,
        61,
    )
    assert music["98"]["inputs"]["seconds"] == 61
    assert music["107"]["inputs"]["filename_prefix"] == "content_pipeline/music/track"
    assert music["3"]["inputs"]["steps"] == 8


@pytest.mark.parametrize("name", sorted(_SETTING_KEYS))
def test_unmarked_copy_of_a_bundle_falls_back_to_ids_with_the_same_payload(
    name: str, tmp_path: Path, frozen_time
):
    bundled = _bundled(name)
    legacy = _without_titles(bundled)
    if name == "music":
        # Older copies of the ACE-Step file carried 16 steps; the engine
        # forced 8 by node id and must keep doing so for unmarked files.
        legacy["3"]["inputs"]["steps"] = 16
    legacy_path = _write(tmp_path / f"{name}.json", legacy)
    assert contract.contract_report(legacy, name)["mode"] == contract.MODE_LEGACY

    from_markers = _prepare(name, Settings(ltx_resolution="1280x704"))
    from_ids = _prepare(
        name,
        Settings(ltx_resolution="1280x704", **{_SETTING_KEYS[name]: str(legacy_path)}),
    )

    assert _without_titles(from_markers) == from_ids


def test_cloud_style_video_workflow_with_other_ids_receives_substitutions(
    tmp_path: Path, frozen_time
):
    workflow = {
        # A decoy that sits on the bundled LoadImage id; marker mode must not
        # touch it.
        "269": {
            "class_type": "LoadImage",
            "inputs": {"image": "decoy.png"},
            "_meta": {"title": "Watermark"},
        },
        "first_frame": {
            "class_type": "LoadImage",
            "inputs": {"image": "placeholder.png"},
            "_meta": {"title": "ENGINE_INPUT_IMAGE"},
        },
        "cloud": {
            "class_type": "CloudImageToVideoNode",
            "inputs": {
                "start_frame": ["first_frame", 0],
                "prompt": "",
                "negative_prompt": "blurry",
                "duration_seconds": 5,
                "seed": 0,
            },
            "_meta": {
                "title": "Cloud i2v ENGINE_PROMPT ENGINE_NEGATIVE ENGINE_DURATION ENGINE_SEED"
            },
        },
        "local_sampler": {
            "class_type": "KSampler",
            "inputs": {"seed": 123},
            "_meta": {"title": "Pinned upscaler sampler"},
        },
        "save": {
            "class_type": "SaveVideo",
            "inputs": {"filename_prefix": "video/cloud", "video": ["cloud", 0]},
            "_meta": {"title": "ENGINE_OUTPUT"},
        },
    }
    path = _write(tmp_path / "cloud_i2v.json", workflow)

    prepared = generation.prepare_ltx_workflow(
        "slow orbit",
        "uploaded.png",
        5,
        duration_seconds=999,
        negative_extra="no neon",
        settings=Settings(comfy_ltx_workflow=str(path)),
    )

    cloud = prepared["cloud"]["inputs"]
    assert prepared["first_frame"]["inputs"]["image"] == "uploaded.png"
    assert prepared["269"]["inputs"]["image"] == "decoy.png"
    assert cloud["prompt"] == "slow orbit"
    assert cloud["negative_prompt"] == "blurry, no neon"
    assert cloud["duration_seconds"] == generation.MAX_LTX_DURATION
    assert cloud["start_frame"] == ["first_frame", 0]
    assert 0 < cloud["seed"] < 2**31
    assert prepared["local_sampler"]["inputs"]["seed"] == 123
    assert prepared["save"]["inputs"]["filename_prefix"].endswith("shot_005_ltx2_3")


def test_cloud_image_and_music_workflows_use_markers_and_explicit_inputs(tmp_path: Path):
    image = _write(
        tmp_path / "cloud_image.json",
        {
            "1": {
                "class_type": "CloudTextToImageNode",
                "inputs": {"text": "unused", "prompt_text": "", "seed": 5},
                "_meta": {"title": "ENGINE_PROMPT:prompt_text"},
            },
            "2": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": "x", "images": ["1", 0]},
                "_meta": {"title": "engine_output"},
            },
        },
    )
    music = _write(
        tmp_path / "cloud_music.json",
        {
            "3": {
                "class_type": "KSampler",
                "inputs": {"steps": 30, "seed": 1},
                "_meta": {"title": "KSampler"},
            },
            "audio": {
                "class_type": "CloudTextToAudioNode",
                "inputs": {"prompt": "", "duration": 10},
                "_meta": {"title": "ENGINE_PROMPT ENGINE_DURATION"},
            },
            "save": {
                "class_type": "SaveAudioMP3",
                "inputs": {"filename_prefix": "audio/x", "audio": ["audio", 0]},
                "_meta": {"title": "ENGINE_OUTPUT"},
            },
        },
    )

    image_workflow = generation.prepare_image_workflow(
        "ideogram4",
        "harbour",
        2,
        settings=Settings(comfy_ideogram_workflow=str(image)),
    )
    music_workflow = generation.prepare_music_workflow(
        "warm piano",
        92,
        45,
        settings=Settings(comfy_ace_workflow=str(music)),
    )

    assert image_workflow["1"]["inputs"]["prompt_text"] == "harbour"
    assert image_workflow["1"]["inputs"]["text"] == "unused"
    assert image_workflow["2"]["inputs"]["filename_prefix"].endswith("shot_002_ideogram4")
    assert music_workflow["audio"]["inputs"] == {"prompt": "warm piano", "duration": 45}
    # The legacy "force 8 steps on node 3" tweak is only for unmarked files.
    assert music_workflow["3"]["inputs"]["steps"] == 30
    assert music_workflow["save"]["inputs"]["filename_prefix"] == "content_pipeline/music/track"


def test_engine_seed_marker_pins_other_seeds_even_in_an_unmarked_copy(
    tmp_path: Path, frozen_time
):
    legacy = _without_titles(_bundled("krea"))
    legacy["30:3"]["_meta"] = {"title": "ENGINE_SEED"}
    legacy["30:16"]["inputs"]["sampling_mode.seed"] = 77
    path = _write(tmp_path / "krea.json", legacy)

    prepared = generation.prepare_image_workflow(
        "krea2_turbo",
        "pier",
        1,
        settings=Settings(comfy_krea_workflow=str(path)),
    )

    assert prepared["30:19"]["inputs"]["value"] == "pier"
    assert prepared["30:16"]["inputs"]["sampling_mode.seed"] == 77
    assert prepared["30:3"]["inputs"]["seed"] == int(FROZEN_TIME * 1000) % 2_147_000_000


def test_generated_seeds_fit_signed_32_bit_api_inputs(monkeypatch):
    workflow = {"a": {"inputs": {"seed": 0, "noise_seed": 0}}}
    monkeypatch.setattr(generation.time, "time", lambda: 4_102_444_800.999)

    generation.set_workflow_seed_values(workflow)

    assert all(0 <= value < 2**31 for value in workflow["a"]["inputs"].values())


def _ltx_marked(**overrides) -> dict:
    workflow = {
        "img": {"inputs": {"image": "a.png"}, "_meta": {"title": "ENGINE_INPUT_IMAGE"}},
        "prompt": {"inputs": {"value": ""}, "_meta": {"title": "ENGINE_PROMPT"}},
        "save": {"inputs": {"filename_prefix": "x"}, "_meta": {"title": "ENGINE_OUTPUT"}},
    }
    workflow.update(overrides)
    return workflow


@pytest.mark.parametrize(
    ("workflow", "expected"),
    [
        (
            {"prompt": {"inputs": {"value": ""}, "_meta": {"title": "ENGINE_PROMPT"}}},
            "no node is marked ENGINE_INPUT_IMAGE",
        ),
        (
            _ltx_marked(
                second={"inputs": {"value": ""}, "_meta": {"title": "ENGINE_PROMPT"}}
            ),
            "ENGINE_PROMPT must mark exactly one node",
        ),
        (
            _ltx_marked(
                encode={
                    "inputs": {"text": ["prompt", 0]},
                    "_meta": {"title": "ENGINE_NEGATIVE"},
                }
            ),
            "connected to node 'prompt'",
        ),
        (
            _ltx_marked(
                kling={
                    "inputs": {"duration": "5"},
                    "_meta": {"title": "ENGINE_DURATION"},
                }
            ),
            "must be a number",
        ),
        (
            _ltx_marked(
                prompt={
                    "inputs": {"text": ""},
                    "_meta": {"title": "ENGINE_PROMPT ENGINE_NEGATIVE"},
                }
            ),
            "ENGINE_PROMPT and ENGINE_NEGATIVE both target input prompt.text",
        ),
        (
            _ltx_marked(
                save={
                    "inputs": {"filename_prefix": "x"},
                    "_meta": {"title": "ENGINE_OUTPUT:prefix"},
                }
            ),
            "names input 'prefix'",
        ),
        (
            {"1": {"inputs": {"image": "x"}, "_meta": {"title": "ENGINE_INPUT_IMAG"}}},
            "unknown marker ENGINE_INPUT_IMAG",
        ),
    ],
)
def test_contract_violations_have_actionable_errors(workflow: dict, expected: str):
    with pytest.raises(contract.WorkflowContractError) as error:
        contract.bind_workflow(workflow, "ltx")

    assert expected in str(error.value)


def test_unmarked_foreign_workflow_names_missing_ids_and_required_markers(tmp_path: Path):
    path = _write(
        tmp_path / "foreign.json",
        {"5": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}},
    )

    with pytest.raises(contract.WorkflowContractError) as error:
        generation.prepare_image_workflow(
            "krea2_turbo",
            "x",
            1,
            settings=Settings(comfy_krea_workflow=str(path)),
        )

    message = str(error.value)
    assert "has no ENGINE_* markers" in message
    assert "node '30:19' is missing" in message
    assert "ENGINE_PROMPT, ENGINE_OUTPUT are required" in message
    assert str(path) in message


def test_unused_markers_are_reported_as_warnings():
    workflow = _bundled("krea")
    workflow["30:6"]["_meta"]["title"] = "ENGINE_NEGATIVE"

    report = contract.contract_report(workflow, "krea")

    assert report["ok"] is True
    assert report["warnings"] == [
        "ENGINE_NEGATIVE on node '30:6' is not used by the Krea image workflow"
    ]


def test_reference_markers_may_carry_a_readable_title(tmp_path: Path):
    path = _write(
        tmp_path / "reference.json",
        {
            "load": {"inputs": {"image": "x"}, "_meta": {"title": "ENGINE_REFERENCE_IMAGE | anchor"}},
            "text": {"inputs": {"prompt": ""}, "_meta": {"title": "Positive · ENGINE_PROMPT"}},
            "save": {"inputs": {"filename_prefix": "x"}, "_meta": {"title": "ENGINE_OUTPUT"}},
        },
    )
    settings = Settings(
        comfy_reference_workflow=str(path),
        reference_conditioning_mode="required",
    )

    workflow = generation.prepare_reference_image_workflow("same room", "canon.png", 4, settings=settings)

    assert workflow["load"]["inputs"]["image"] == "canon.png"
    assert workflow["text"]["inputs"]["prompt"] == "same room"


def test_reference_workflow_without_markers_has_no_id_fallback(tmp_path: Path):
    path = _write(tmp_path / "reference.json", {"1": {"inputs": {"image": "x"}}})
    settings = Settings(
        comfy_reference_workflow=str(path),
        reference_conditioning_mode="required",
    )

    status = generation.reference_workflow_status(settings)

    assert status["ok"] is False
    assert "no node is marked ENGINE_REFERENCE_IMAGE" in status["error"]


def _object_info_for_bundles() -> dict:
    info: dict = {}
    for path in comfy.DEFAULT_WORKFLOW_PATHS.values():
        for node in json.loads(path.read_text(encoding="utf-8")).values():
            info[node["class_type"]] = {}
    return info


def _broken_ltx(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "custom_ltx.json",
        {
            "10": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": ""},
                "_meta": {"title": "ENGINE_PROMPT"},
            }
        },
    )


def test_configured_workflow_validation_reports_contract_errors(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(comfy, "comfy_request_json", lambda *_a, **_k: _object_info_for_bundles())
    settings = Settings(comfy_ltx_workflow=str(_broken_ltx(tmp_path)))

    report = comfy.validate_configured_workflows("http://comfy", settings)

    assert report["ok"] is False
    ltx = report["workflows"]["ltx"]
    assert ltx["ok"] is False
    assert ltx["missing_node_types"] == []
    assert "no node is marked ENGINE_INPUT_IMAGE" in ltx["error"]
    assert report["workflows"]["krea"]["ok"] is True
    assert report["workflows"]["krea"]["contract"]["mode"] == contract.MODE_MARKERS


def test_comfy_diagnostic_surfaces_contract_errors_before_render(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(diagnostics, "_http_get", lambda *_a, **_k: (True, "{}"))
    monkeypatch.setattr(comfy, "comfy_request_json", lambda *_a, **_k: _object_info_for_bundles())
    settings = Settings(comfy_ltx_workflow=str(_broken_ltx(tmp_path)))

    result = diagnostics._check_comfy(settings)

    assert result.ok is False
    assert "ltx:" in result.detail
    assert "ENGINE_INPUT_IMAGE" in result.detail


def test_material_preflight_blocks_a_workflow_that_breaks_the_contract(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(assets_router, "_required_material_workflows", lambda *_a: ["ltx"])
    monkeypatch.setattr(
        assets_router.comfy_client,
        "comfy_request_json",
        lambda *_a, **_k: _object_info_for_bundles(),
    )
    settings = Settings(comfy_ltx_workflow=str(_broken_ltx(tmp_path)))

    result = assets_router._material_workflow_preflight(tmp_path, settings)

    assert result["ok"] is False
    assert "ENGINE_INPUT_IMAGE" in result["workflows"]["ltx"]["error"]
