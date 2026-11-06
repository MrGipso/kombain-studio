"""MiniMax H3 as the second image-to-video engine.

H3 runs from the same local ComfyUI as LTX.  The engine is a global setting;
the prompt is wrapped into the structured I2VA format of MiniMax's official
prompt-writing guide; clip length is a frame count on H3's 17k+5 grid.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.routers import assets as assets_router
from core import comfy
from core import comfy_contract as contract
from core import comfy_generation as generation
from core import shot_planning
from core.config import Settings


@pytest.fixture(autouse=True)
def _isolated_vram_state(monkeypatch):
    freed: list[str] = []
    monkeypatch.setattr(generation, "_last_vram_family", {})
    monkeypatch.setattr(generation, "comfy_free_memory", lambda url: freed.append(url) or True)
    return freed


def _bundled_h3() -> dict:
    return json.loads(comfy.DEFAULT_WORKFLOW_PATHS["h3"].read_text(encoding="utf-8"))


def _image(path: Path, size: tuple[int, int]) -> Path:
    Image.new("RGB", size, (40, 60, 80)).save(path)
    return path


def _card(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = dict(
        image_output=str(_image(tmp_path / "frame.png", (1344, 768))),
        video_prompt="A lamp flickers while the camera pushes in with small amplitude at slow speed.",
        image_prompt="A dusty workshop bench under a hanging lamp. Tools hang on the wall.",
        sfx_query="workshop room tone",
        shot_index=4,
        start="00:00:01",
        end="00:00:08",
        priority="medium",
        ltx_draft=False,
        video_model="ltx2.3_i2v",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# --------------------------------------------------------------------------
# Frames, resolution, prompt
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "frames"),
    [
        (0, 124),
        (2.0, 124),
        (5.0, 124),
        (124 / 24, 124),
        (5.2, 141),
        (7.4, 192),
        (10.0, 243),
        (14.9, 362),
        (40.0, 362),
        (float("nan"), 124),
        ("bad", 124),
    ],
)
def test_frames_follow_the_17k_plus_5_grid_within_the_trained_range(seconds, frames):
    result = generation.h3_frames_for(seconds)

    assert result == frames
    assert result % 17 == 5
    assert generation.H3_MIN_FRAMES <= result <= generation.H3_MAX_FRAMES


def test_frames_cover_the_requested_duration_when_in_range():
    for tenths in range(52, 150):
        seconds = tenths / 10
        assert generation.h3_frames_for(seconds) / generation.H3_FPS >= seconds


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((1920, 1080), (1344, 768)),
        ((1080, 1920), (768, 1344)),
        ((1024, 1024), (1024, 1024)),
        ((1100, 1000), (1024, 1024)),
    ],
)
def test_resolution_follows_the_first_frame_orientation(tmp_path, size, expected):
    assert generation.h3_render_resolution(_image(tmp_path / "f.png", size)) == expected


def test_unreadable_first_frame_renders_landscape(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")

    assert generation.h3_render_resolution(broken) == (1344, 768)
    assert generation.h3_render_resolution(tmp_path / "missing.png") == (1344, 768)


def test_prompt_uses_the_official_i2va_layout():
    prompt = generation.compose_h3_prompt(
        "A lamp flickers; the camera pushes in with small amplitude at slow speed",
        first_frame="A dusty workshop bench under a hanging lamp. Tools hang on the wall.",
        soundscape="workshop room tone.",
    )

    first_line, rest = prompt.split("\n\n", 1)
    assert first_line == (
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced."
    )
    blocks = rest.split("\n\n")
    assert [block.split(":", 1)[0] for block in blocks] == [
        "integrated_multimodal_description",
        "overall_soundscape",
        "non_diegetic_music",
    ]
    description = blocks[0]
    assert description.startswith("integrated_multimodal_description: [Shot 1] ")
    assert description.count("[Shot") == 1
    # Only the first sentence of the image prompt anchors the first frame.
    assert "A dusty workshop bench under a hanging lamp." in description
    assert "Tools hang" not in description
    assert "slow speed." in description
    assert "one continuous shot" in description
    assert blocks[1] == "overall_soundscape: The only sound is workshop room tone."
    assert blocks[2] == "non_diegetic_music: N/A"


def test_prompt_strips_on_screen_text_quotes_and_control_tags():
    prompt = generation.compose_h3_prompt(
        'A sign reads "OPEN" and «SALE» <d>[English] hi</d> while it sways',
        first_frame='A “neon” shop front',
    )

    body = prompt.split("\n\n", 1)[1]
    for token in ('"', "“", "”", "«", "»", "<d>", "</d>"):
        assert token not in body
    assert "A sign reads OPEN and SALE [English] hi while it sways." in body
    assert body.count("<Picture") == 0


def test_prompt_falls_back_to_a_neutral_motion_and_soundscape():
    prompt = generation.compose_h3_prompt("", first_frame="")

    assert "The video opens exactly on the first frame. " in prompt
    assert generation.H3_DEFAULT_MOTION in prompt
    assert "overall_soundscape: The only sound is quiet natural ambience of this place." in prompt


# --------------------------------------------------------------------------
# Workflow contract and payload
# --------------------------------------------------------------------------


def test_bundled_h3_workflow_is_marked_and_writes_the_template_node_ids():
    workflow = generation.prepare_h3_workflow(
        "the prompt",
        "shot_input.png",
        7,
        frames=192,
        width=768,
        height=1344,
        turbo=True,
        settings=Settings(),
    )

    node = workflow["105:104"]["inputs"]
    assert (node["prompt"], node["length"], node["width"], node["height"]) == (
        "the prompt",
        192,
        768,
        1344,
    )
    assert workflow["114"]["inputs"]["image"] == "shot_input.png"
    assert workflow["105:126"]["inputs"]["value"] is True
    assert workflow["92"]["inputs"]["filename_prefix"] == (
        "content_pipeline/video/shot_007_minimax_h3"
    )
    assert workflow["105:15"]["inputs"]["noise_seed"] < 2**31


def test_unmarked_official_template_export_binds_by_node_ids(tmp_path):
    clean = copy.deepcopy(_bundled_h3())
    for node in clean.values():
        node.pop("_meta", None)

    report = contract.contract_report(clean, "h3")

    assert report["ok"] is True
    assert report["mode"] == contract.MODE_LEGACY
    assert report["roles"][contract.TURBO] == ["105:126.value"]
    assert report["roles"][contract.FRAMES] == ["105:104.length"]


def test_turbo_marker_requires_a_boolean_input():
    workflow = _bundled_h3()
    workflow["105:126"]["inputs"]["value"] = 1

    report = contract.contract_report(workflow, "h3")

    assert report["ok"] is False
    assert "must be a boolean" in report["error"]


def test_custom_workflow_without_turbo_never_produces_a_draft(monkeypatch, tmp_path):
    workflow = _bundled_h3()
    workflow["105:126"]["_meta"]["title"] = "Boolean (Enable Lightning LoRA)"
    custom = tmp_path / "h3_custom.json"
    custom.write_text(json.dumps(workflow), encoding="utf-8")
    settings = Settings(
        video_engine="minimax_h3",
        comfy_h3_workflow=str(custom),
        ltx_draft_mode="all",
    )
    queued = _stub_render(monkeypatch, tmp_path)
    card = _card(tmp_path)

    generation.generate_comfy_h3_video("http://comfy", tmp_path, card, settings=settings)

    assert card.ltx_draft is False
    assert queued[0]["105:126"]["inputs"]["value"] is False


# --------------------------------------------------------------------------
# Rendering and engine dispatch
# --------------------------------------------------------------------------


def _stub_render(monkeypatch, tmp_path: Path) -> list[dict]:
    queued: list[dict] = []

    def fake_queue(_url, workflow, *_args, **_kwargs):
        queued.append(workflow)
        return f"prompt-{len(queued)}"

    monkeypatch.setattr(generation, "comfy_upload_image", lambda *_args: "uploaded.png")
    monkeypatch.setattr(generation, "comfy_queue_prompt", fake_queue)
    monkeypatch.setattr(
        generation,
        "comfy_wait_for_history",
        lambda *_args, **_kwargs: {"outputs": {"92": {"videos": [{"filename": "h3.mp4"}]}}},
    )
    monkeypatch.setattr(
        generation,
        "comfy_download_output",
        lambda _url, _item, folder, index, prefix: folder / f"shot_{index:03d}_{prefix}.mp4",
    )
    return queued


def test_h3_render_sends_the_structured_prompt_frames_and_size(monkeypatch, tmp_path):
    queued = _stub_render(monkeypatch, tmp_path)
    card = _card(
        tmp_path,
        image_output=str(_image(tmp_path / "portrait.png", (1080, 1920))),
    )

    path = generation.generate_comfy_h3_video(
        "http://comfy", tmp_path, card, draft=False, settings=Settings(video_engine="minimax_h3")
    )

    assert path.name == "shot_004_minimax_h3.mp4"
    node = queued[0]["105:104"]["inputs"]
    assert node["prompt"].startswith(generation.H3_FIRST_FRAME_LINE + "\n\n")
    assert "A lamp flickers" in node["prompt"]
    assert "The only sound is workshop room tone." in node["prompt"]
    assert node["length"] == 175  # 7 s = 168 frames -> 175 on the 17k+5 grid
    assert (node["width"], node["height"]) == (768, 1344)
    assert queued[0]["114"]["inputs"]["image"] == "uploaded.png"
    assert queued[0]["105:126"]["inputs"]["value"] is False
    assert card.ltx_draft is False


@pytest.mark.parametrize(
    ("mode", "priority", "draft", "turbo"),
    [
        ("off", "medium", None, False),
        ("all", "high", None, True),
        ("auto", "medium", None, True),
        ("auto", "high", None, False),
        ("all", "medium", False, False),
    ],
)
def test_draft_policy_switches_the_turbo_lora(monkeypatch, tmp_path, mode, priority, draft, turbo):
    queued = _stub_render(monkeypatch, tmp_path)
    card = _card(tmp_path, priority=priority)

    generation.generate_comfy_h3_video(
        "http://comfy",
        tmp_path,
        card,
        draft,
        settings=Settings(video_engine="minimax_h3", ltx_draft_mode=mode),
    )

    assert queued[0]["105:126"]["inputs"]["value"] is turbo
    assert card.ltx_draft is turbo


def test_h3_render_requires_the_shot_image(tmp_path):
    card = _card(tmp_path, image_output=str(tmp_path / "missing.png"))

    with pytest.raises(FileNotFoundError, match="MiniMax H3"):
        generation.generate_comfy_h3_video("http://comfy", tmp_path, card, settings=Settings())


def test_dispatcher_picks_the_engine_and_records_the_model(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(
        generation,
        "generate_comfy_h3_video",
        lambda *_args, **_kwargs: calls.append("h3") or tmp_path / "h3.mp4",
    )
    monkeypatch.setattr(
        generation,
        "generate_comfy_ltx_video",
        lambda *_args, **_kwargs: calls.append("ltx") or tmp_path / "ltx.mp4",
    )
    card = _card(tmp_path, video_model="ltx2.3_i2v")

    generation.generate_comfy_video(
        "http://comfy", tmp_path, card, settings=Settings(video_engine="minimax_h3")
    )
    assert card.video_model == generation.H3_VIDEO_MODEL

    generation.generate_comfy_video("http://comfy", tmp_path, card, settings=Settings())
    assert card.video_model == generation.LTX_VIDEO_MODEL
    assert calls == ["h3", "ltx"]


def test_unknown_engine_value_falls_back_to_ltx():
    assert comfy.video_engine({"video_engine": "sora"}) == comfy.VIDEO_ENGINE_LTX
    assert comfy.video_engine(None) == comfy.VIDEO_ENGINE_LTX
    assert generation.video_model_id({"video_engine": " MiniMax_H3 "}) == generation.H3_VIDEO_MODEL


def test_vram_is_freed_only_when_switching_into_or_out_of_h3(
    monkeypatch, tmp_path, _isolated_vram_state
):
    freed = _isolated_vram_state
    _stub_render(monkeypatch, tmp_path)
    h3 = Settings(video_engine="minimax_h3")

    generation._queue_prompt("http://comfy", {"krea": {}}, h3)
    assert freed == []  # unknown -> image: nothing big to clear

    generation.generate_comfy_h3_video("http://comfy", tmp_path, _card(tmp_path), settings=h3)
    generation.generate_comfy_h3_video("http://comfy", tmp_path, _card(tmp_path), settings=h3)
    assert freed == ["http://comfy"]  # one unload for a run of H3 clips

    generation._queue_prompt("http://comfy/", {"krea": {}}, h3)
    assert freed == ["http://comfy", "http://comfy/"]  # back out of H3


def test_first_h3_render_after_start_frees_vram(monkeypatch, tmp_path, _isolated_vram_state):
    _stub_render(monkeypatch, tmp_path)

    generation.generate_comfy_h3_video(
        "http://comfy", tmp_path, _card(tmp_path), settings=Settings(video_engine="minimax_h3")
    )

    assert _isolated_vram_state == ["http://comfy"]


def test_free_memory_posts_unload_and_never_raises(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return b""

    def fake_urlopen(request, timeout):
        seen.append((request.full_url, json.loads(request.data), timeout))
        return Response()

    monkeypatch.setattr(comfy.urllib.request, "urlopen", fake_urlopen)
    assert comfy.comfy_free_memory("http://comfy/") is True
    assert seen == [("http://comfy/free", {"unload_models": True, "free_memory": True}, 30)]

    def failing(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(comfy.urllib.request, "urlopen", failing)
    assert comfy.comfy_free_memory("http://comfy") is False


# --------------------------------------------------------------------------
# Diagnostics, preflight, planner
# --------------------------------------------------------------------------


def test_workflow_paths_follow_the_engine(tmp_path):
    custom = tmp_path / "h3.json"

    ltx = comfy.configured_workflow_paths(Settings())
    h3 = comfy.configured_workflow_paths(
        Settings(video_engine="minimax_h3", comfy_h3_workflow=str(custom))
    )

    assert {"ltx", "ltx_draft"} <= set(ltx) and "h3" not in ltx
    assert h3["h3"] == custom.resolve()
    assert "ltx" not in h3 and "ltx_draft" not in h3
    assert {"krea", "ideogram", "music"} <= set(h3)


@pytest.mark.parametrize(("engine", "workflow"), [("ltx", "ltx"), ("minimax_h3", "h3")])
def test_material_preflight_requires_the_engine_workflow(monkeypatch, tmp_path, engine, workflow):
    card = SimpleNamespace(
        visual_type="ai_image_then_i2v",
        image_model="krea2_turbo",
        status="pending",
        image_output="",
        video_output="",
        assigned_media="",
    )
    monkeypatch.setattr(assets_router, "_load_asset_board", lambda _pdir: [card])

    required = assets_router._required_material_workflows(
        tmp_path, Settings(video_engine=engine)
    )

    assert workflow in required
    assert ({"ltx", "h3"} - {workflow}).isdisjoint(required)


def test_planner_uses_h3_rules_only_for_the_h3_engine():
    ltx = shot_planning.build_shot_instructions()
    h3 = shot_planning.build_shot_instructions(video_engine="minimax_h3")

    assert shot_planning.LTX_VIDEO_PROMPT_RULES in ltx
    assert "MiniMax H3" not in ltx
    assert shot_planning.H3_VIDEO_PROMPT_RULES in h3
    assert "LTX 2.3 image-to-video" not in h3
    assert "with small amplitude" in h3 and "at slow speed" in h3
    assert ltx.replace(shot_planning.LTX_VIDEO_PROMPT_RULES, "") == h3.replace(
        shot_planning.H3_VIDEO_PROMPT_RULES, ""
    )


def test_engine_change_is_an_asset_input_but_not_a_shot_input():
    from backend.routers import settings as settings_router
    import inspect

    source = inspect.getsource(settings_router.update_settings)
    asset_block = source.split("asset_inputs = {", 1)[1].split("}", 1)[0]
    shot_block = source.split("shot_inputs = {", 1)[1].split("}", 1)[0]
    assert '"video_engine"' in asset_block and '"comfy_h3_workflow"' in asset_block
    assert "video_engine" not in shot_block
