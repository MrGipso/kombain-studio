from __future__ import annotations

import json

import pytest

from backend.services import pipeline_orch
from core.models import Shot


def _scene() -> dict:
    return {
        "index": 1,
        "start": "00:00:00",
        "end": "00:00:10",
        "title": "A ship at sea",
        "voice_text": "A heavy ship remains afloat.",
        "visual_direction": "A knitted ship on yarn waves",
        "screen_text": "",
        "music_note": "",
        "retention_note": "",
    }


def _shot() -> Shot:
    return Shot(
        index=1,
        scene_index=1,
        start="00:00:00",
        end="00:00:10",
        visual_type="still_kenburns",
        description="A knitted ship crosses a yarn sea",
        asset_query="knitted ship yarn sea",
        image_model="krea2_turbo",
        video_model="",
        image_prompt="A knitted ship crosses a textured blue yarn sea.",
        video_prompt="",
        screen_text="",
        motion="slow push in",
        priority="high",
        fallback="",
    )


def _plan() -> dict:
    return {
        "scene_index": 1,
        "narrative_role": "introduction",
        "visual_family": "observational_broll",
        "location": "open ocean",
        "primary_subject": "knitted ship",
        "shot_progression": "wide to detail",
        "palette": "blue and grey",
        "transition": "fade",
        "avoid": "repetition",
        "story_thread": "buoyancy",
        "continuity_mode": "same_location",
        "location_id": "location-open-ocean",
        "subject_ids": "subject-knitted-ship",
        "entry_state": "ship underway",
        "scene_event": "ship crosses the waves",
        "exit_state": "ship remains afloat",
    }


def test_final_annotated_candidate_replaces_raw_partial_before_context_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    (tmp_path / "01_transcript_segments.json").write_text(
        json.dumps([
            {"start": "00:00:00", "end": "00:00:10", "text": "Narration"}
        ]),
        encoding="utf-8",
    )

    def fake_generate(*_args, on_visual_plan=None, **_kwargs):
        assert on_visual_plan is not None
        on_visual_plan([_plan()])
        return [_shot()]

    monkeypatch.setattr(
        pipeline_orch.shot_planner,
        "call_lm_studio_for_shots",
        fake_generate,
    )
    monkeypatch.setattr(pipeline_orch, "_ensure_lm_context", lambda *_args: None)
    monkeypatch.setattr(
        pipeline_orch,
        "_maybe_unload_lm",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        pipeline_orch.continuity,
        "build_context_bible",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("context failed")),
    )

    with pytest.raises(RuntimeError, match="context failed"):
        pipeline_orch.run_shot_generation(
            tmp_path,
            [_scene()],
            {"shot_mode": "ai", "route_priority": "economy"},
        )

    checkpoint = json.loads(
        (tmp_path / "04_shot_list.partial.json").read_text("utf-8")
    )
    assert checkpoint[0]["start"] == "00:00:00"
    assert checkpoint[-1]["end"] == "00:00:10"
    assert checkpoint[0]["segment_ids"] == [1]
    assert checkpoint[0]["visual_concept_id"] == "scene-001-shot-001"


def test_shot_failure_unloads_the_exact_model_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    (tmp_path / "01_transcript_segments.json").write_text("[]", encoding="utf-8")
    unloaded: list[str | None] = []
    monkeypatch.setattr(
        pipeline_orch,
        "_ensure_lm_context",
        lambda *_args: "model:2",
    )
    monkeypatch.setattr(
        pipeline_orch,
        "_maybe_unload_lm",
        lambda *_args, model=None, **_kwargs: unloaded.append(model),
    )
    monkeypatch.setattr(
        pipeline_orch.shot_planner,
        "call_lm_studio_for_shots",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("LM failed")),
    )

    with pytest.raises(RuntimeError, match="LM failed"):
        pipeline_orch.run_shot_generation(
            tmp_path,
            [_scene()],
            {"shot_mode": "ai", "route_priority": "economy"},
        )

    assert unloaded == ["model:2"]


def test_global_visual_plan_is_reused_when_scenes_and_settings_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    (tmp_path / "01_transcript_segments.json").write_text(
        json.dumps([
            {"start": "00:00:00", "end": "00:00:10", "text": "Narration"}
        ]),
        encoding="utf-8",
    )
    supplied_cache: list[object] = []

    def fake_generate(*_args, on_visual_plan=None, **kwargs):
        cached = kwargs.get("precomputed_global_visual_plan")
        supplied_cache.append(cached)
        assert on_visual_plan is not None
        on_visual_plan(cached or [_plan()])
        return [_shot()]

    monkeypatch.setattr(
        pipeline_orch.shot_planner,
        "call_lm_studio_for_shots",
        fake_generate,
    )
    monkeypatch.setattr(pipeline_orch, "_ensure_lm_context", lambda *_args: None)
    monkeypatch.setattr(
        pipeline_orch,
        "_maybe_unload_lm",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        pipeline_orch.continuity,
        "build_context_bible",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop after plan")),
    )
    settings = {
        "lm_model": "model",
        "video_style": "knitted",
        "shot_mode": "ai",
        "route_priority": "economy",
    }

    for _ in range(2):
        with pytest.raises(RuntimeError, match="stop after plan"):
            pipeline_orch.run_shot_generation(
                tmp_path,
                [_scene()],
                settings,
            )
    changed_settings = {**settings, "video_style": "documentary"}
    with pytest.raises(RuntimeError, match="stop after plan"):
        pipeline_orch.run_shot_generation(
            tmp_path,
            [_scene()],
            changed_settings,
        )

    assert supplied_cache[0] is None
    assert isinstance(supplied_cache[1], list)
    assert supplied_cache[1][0]["scene_index"] == 1
    assert supplied_cache[2] is None
