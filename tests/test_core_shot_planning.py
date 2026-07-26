from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import shot_planning as planning
from core.models import Scene, Shot, TranscriptSegment


def _scene(start: str = "00:00:00", end: str = "00:00:10") -> Scene:
    return Scene(
        index=1,
        start=start,
        end=end,
        title="Server room",
        voice_text="Data moves through a server room.",
        visual_direction="Wide server room with rows of active racks",
        screen_text="",
        music_note="",
        retention_note="",
    )


def _shot(**overrides) -> Shot:
    values = {
        "index": 1,
        "scene_index": 1,
        "start": "00:00:00",
        "end": "00:00:10",
        "visual_type": "ai_image_then_i2v",
        "description": "A server room with active racks",
        "asset_query": "server room racks",
        "image_model": "krea2_turbo",
        "video_model": "ltx2.3_i2v",
        "image_prompt": "A wide server room under cold fluorescent light.",
        "video_prompt": "The camera slowly tracks between the server racks.",
        "screen_text": "NETWORK",
        "motion": "slow tracking movement",
        "priority": "medium",
        "fallback": "AI image-to-video generation",
    }
    values.update(overrides)
    return Shot(**values)


def _raw_shot(**overrides) -> dict:
    values = {
        "scene_index": 1,
        "start": "00:00:01",
        "end": "00:00:09",
        "visual_type": "unexpected-route",
        "description": "A server room with active racks",
        "asset_query": "server room racks",
        "image_model": "",
        "video_model": "",
        "image_prompt": "A wide server room under cold fluorescent light.",
        "video_prompt": "The camera slowly tracks between the server racks.",
        "screen_text": "",
        "motion": "slow tracking movement",
        "priority": "high",
        "fallback": "",
        "sfx_query": "server room fan hum",
        "reference_query": "",
        "reference_source": "wikipedia",
    }
    values.update(overrides)
    return values


def _global_plan(scenes: list[Scene]) -> list[planning.GlobalVisualPlanItem]:
    families = planning.GLOBAL_VISUAL_FAMILIES
    return [
        planning.GlobalVisualPlanItem(
            scene_index=scene.index,
            narrative_role=f"Роль сцены {scene.index}",
            visual_family=families[(scene.index - 1) % len(families)],
            location=f"Уникальная локация {scene.index}",
            primary_subject=f"Главный объект {scene.index}",
            shot_progression="Общий план, действие, предметная деталь",
            palette=f"Палитра {scene.index}",
            transition=f"Переход к сцене {scene.index + 1}",
            avoid=f"Не повторять объект предыдущей сцены {scene.index}",
        )
        for scene in scenes
    ]


def _stub_global_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        planning,
        "create_global_visual_plan",
        lambda _base_url, _model, scenes, **_kwargs: _global_plan(scenes),
    )


def test_module_import_is_headless_and_does_not_load_global_config():
    root = Path(__file__).resolve().parents[1]
    script = r"""
import builtins
import core.config
def fail_load(*args, **kwargs):
    raise AssertionError('shot planning read global settings')
core.config.load_settings = fail_load
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    blocked = (
        name == 'content_pipeline_gui'
        or name == 'backend'
        or name.startswith('backend.')
        or name == 'tkinter'
        or name.startswith('tkinter.')
    )
    if blocked:
        raise AssertionError(f'core shot planning imported {name}')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import core.shot_planning
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_reference_density_is_explicit_with_compatible_normal_default():
    parameter = inspect.signature(
        planning.call_lm_studio_for_shots
    ).parameters["reference_density"]

    assert parameter.default == "normal"
    default_prompt = planning.build_shot_instructions()
    off_prompt = planning.build_shot_instructions(reference_density="off")
    sparse_prompt = planning.build_shot_instructions(reference_density="sparse")
    assert "web_reference_card" in default_prompt
    assert "web_reference_card" not in off_prompt
    assert "МАКСИМУМ 1 такой кадр на 2 сцены" in sparse_prompt


def test_lm_request_uses_canonical_client_and_preserves_transport_contract(monkeypatch):
    captured = {}

    def fake_chat(settings, messages, **kwargs):
        captured.update(settings=settings, messages=messages, kwargs=kwargs)
        return '```json\n[{"scene_index": 1}]\n```'

    monkeypatch.setattr(planning.lm, "chat", fake_chat)

    result = planning.request_lm_shots(
        "http://lm.test/v1/",
        " local-model.gguf ",
        "plan this",
        ttl_seconds=37,
    )

    assert result == [{"scene_index": 1}]
    assert captured["settings"].lm_url == "http://lm.test/v1"
    assert captured["settings"].lm_model == "local-model.gguf"
    assert captured["messages"][1] == {"role": "user", "content": "plan this"}
    assert captured["kwargs"] == {
        "temperature": 0.25,
        "timeout": 300,
        "ttl": 37,
    }


def test_truncated_lm_array_recovers_last_complete_object():
    response = '[{"scene_index": 1}, {"scene_index": 2, "description": "cut'

    assert planning.parse_lm_json_array(response) == [{"scene_index": 1}]


def test_segment_planning_retries_weak_motion_then_applies_postprocessing(monkeypatch):
    _stub_global_plan(monkeypatch)
    requests: list[str] = []
    weak = _raw_shot(video_prompt="", motion="")
    strong = _raw_shot()

    def fake_request(_base_url, _model, prompt, ttl_seconds=None):
        requests.append(prompt)
        return [weak] if len(requests) == 1 else [strong]

    callback_snapshot: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(planning, "request_lm_shots", fake_request)

    shots = planning.call_lm_studio_for_shots(
        "http://lm.test/v1",
        "model",
        [_scene()],
        segments=[
            TranscriptSegment("00:00:00", "00:00:05", "Data moves."),
            TranscriptSegment("00:00:05", "00:00:10", "Racks respond."),
        ],
        on_scene_shots=lambda scene_shots: callback_snapshot.append(
            [(shot.start, shot.end) for shot in scene_shots]
        ),
        video_style="documentary",
        reference_density="off",
    )

    assert len(requests) == 2
    assert "ВАЖНО про video_prompt" in requests[1]
    assert callback_snapshot == [[("00:00:01", "00:00:09")]]
    assert len(shots) == 1
    assert (shots[0].start, shots[0].end) == ("00:00:00", "00:00:10")
    assert shots[0].visual_type == "ai_image_then_i2v"
    assert shots[0].image_model == "krea2_turbo"
    assert shots[0].video_model == "ltx2.3_i2v"
    assert shots[0].fallback == "AI image-to-video generation"
    assert "Rendered " in shots[0].image_prompt
    assert "Animation style:" in shots[0].video_prompt


def test_bad_scene_response_falls_back_without_double_composing_style(monkeypatch):
    _stub_global_plan(monkeypatch)
    calls = 0
    logs: list[str] = []

    def fail_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("broken JSON")

    monkeypatch.setattr(planning, "request_lm_shots", fail_request)

    shots = planning.call_lm_studio_for_shots(
        "http://lm.test/v1",
        "model",
        [_scene(end="00:00:08")],
        segments=[
            TranscriptSegment("00:00:00", "00:00:04", "Data moves."),
            TranscriptSegment("00:00:04", "00:00:08", "Racks respond."),
        ],
        log_callback=logs.append,
        video_style="documentary",
    )

    assert calls == 3
    assert len(shots) == 2
    assert logs and "запасную раскадровку" in logs[0]
    assert all(shot.image_prompt.count("Rendered ") == 1 for shot in shots)
    assert [(shot.start, shot.end) for shot in shots] == [
        ("00:00:00", "00:00:04"),
        ("00:00:04", "00:00:08"),
    ]


def test_long_shot_splits_on_transcript_boundaries_and_keeps_overlay_on_part_a():
    scene = _scene(end="00:00:40")
    segments = [
        TranscriptSegment("00:00:00", "00:00:08", "one"),
        TranscriptSegment("00:00:08", "00:00:16", "two"),
        TranscriptSegment("00:00:16", "00:00:23", "three"),
        TranscriptSegment("00:00:23", "00:00:31", "four"),
        TranscriptSegment("00:00:31", "00:00:40", "five"),
    ]

    parts = planning.split_long_shot(
        _shot(end="00:00:40"),
        scene,
        segments,
    )

    assert [(part.start, part.end) for part in parts] == [
        ("00:00:00", "00:00:16"),
        ("00:00:16", "00:00:31"),
        ("00:00:31", "00:00:40"),
    ]
    assert [part.screen_text for part in parts] == ["NETWORK", "", ""]
    assert all(
        "Continuation shot of the same scene" in part.image_prompt
        for part in parts[1:]
    )


def test_timing_and_route_normalization_match_desktop_characterization():
    shots = [
        _shot(index=8, start="00:00:09", end="00:00:11"),
        _shot(index=4, start="00:00:03", end="00:00:04"),
        _shot(index=5, start="00:00:05", end="00:00:07"),
    ]

    tiled = planning.retile_shots_contiguous(shots, total_end=12)

    assert [(shot.index, shot.start, shot.end) for shot in tiled] == [
        (1, "00:00:00", "00:00:05"),
        (2, "00:00:05", "00:00:09"),
        (3, "00:00:09", "00:00:12"),
    ]

    stock = _shot(visual_type="stock_video", image_model="legacy", video_model="legacy")
    still = _shot(visual_type="still_kenburns", image_model="", video_model="legacy")
    reference = _shot(visual_type="web_reference_card", image_model="legacy", video_model="legacy")
    unknown = _shot(visual_type="unknown", image_model="", video_model="", fallback="")
    planning.normalize_shot_routes([stock, still, reference, unknown], "Ideogram")

    assert (stock.image_model, stock.video_model) == ("legacy", "legacy")
    assert (still.image_model, still.video_model) == ("ideogram4", "")
    assert (reference.image_model, reference.video_model) == ("", "")
    assert unknown.visual_type == "ai_image_then_i2v"
    assert (unknown.image_model, unknown.video_model) == ("ideogram4", "ltx2.3_i2v")


def test_final_scheduler_fills_long_scene_without_stretching_last_shot():
    scene = _scene(end="00:04:40")
    shots = [
        _shot(index=index + 1, start=f"00:00:{start:02d}", end=f"00:00:{start + 4:02d}")
        for index, start in enumerate((0, 12, 24, 36, 48, 57))
    ]
    segments = [
        TranscriptSegment(
            planning.seconds_to_timecode(start),
            planning.seconds_to_timecode(min(start + 10, 280)),
            f"Фрагмент {start // 10}.",
        )
        for start in range(0, 280, 10)
    ]

    finalized = planning.finalize_shot_timeline(shots, [scene], segments)
    intervals = [
        (
            planning.timecode_to_seconds(shot.start),
            planning.timecode_to_seconds(shot.end),
        )
        for shot in finalized
    ]

    assert intervals[0][0] == 0
    assert intervals[-1][1] == 280
    assert all(left[1] == right[0] for left, right in zip(intervals, intervals[1:]))
    # Transcript boundaries may be accepted up to two seconds beyond the
    # nominal 15-second generation limit, but a multi-minute tail is forbidden.
    assert max(end - start for start, end in intervals) <= planning.SHOT_MAX_SECONDS + 2
    assert len(finalized) > len(shots)
    assert [shot.index for shot in finalized] == list(range(1, len(finalized) + 1))


def test_final_scheduler_splits_sparse_subtitles_at_exact_duration_limit():
    finalized = planning.finalize_shot_timeline(
        [_shot(end="00:01:00")],
        [_scene(end="00:01:00")],
        [],
    )

    durations = [
        planning.timecode_to_seconds(shot.end)
        - planning.timecode_to_seconds(shot.start)
        for shot in finalized
    ]
    assert durations == [15, 15, 15, 15]


def test_per_scene_planner_receives_global_map_and_used_concept_memory(monkeypatch):
    _stub_global_plan(monkeypatch)
    prompts: list[str] = []
    second_scene = Scene(
        index=2,
        start="00:00:10",
        end="00:00:20",
        title="Quality control",
        voice_text="Inspectors verify every printed sheet.",
        visual_direction="Inspectors examine banknote sheets under bright light",
        screen_text="",
        music_note="",
        retention_note="",
    )

    def fake_request(_base_url, _model, prompt, ttl_seconds=None):
        prompts.append(prompt)
        return [_raw_shot()]

    monkeypatch.setattr(planning, "request_lm_shots", fake_request)

    planning.call_lm_studio_for_shots(
        "http://lm.test/v1",
        "model",
        [_scene(), second_scene],
        segments=[
            TranscriptSegment("00:00:00", "00:00:10", "Data moves."),
            TranscriptSegment("00:00:10", "00:00:20", "Inspectors verify sheets."),
        ],
        video_style="documentary",
        reference_density="off",
    )

    assert len(prompts) == 2
    assert all("Server room" in prompt and "Quality control" in prompt for prompt in prompts)
    assert "УЖЕ ИСПОЛЬЗОВАННЫЕ ВИЗУАЛЬНЫЕ КОНЦЕПТЫ" in prompts[1]
    assert "server room racks" in prompts[1]


def test_global_visual_plan_parser_and_audit_accept_diverse_complete_plan():
    source_scenes = [
        Scene(
            index=index,
            start=f"00:00:{(index - 1) * 10:02d}",
            end=f"00:00:{index * 10:02d}",
            title=f"Scene {index}",
            voice_text=f"Narration {index}.",
            visual_direction="",
            screen_text="",
            music_note="",
            retention_note="",
        )
        for index in range(1, 6)
    ]
    payload = json.dumps(
        [item.as_dict() for item in _global_plan(source_scenes)],
        ensure_ascii=False,
    )

    parsed = planning.parse_global_visual_plan(payload, source_scenes)

    assert [item.scene_index for item in parsed] == [1, 2, 3, 4, 5]
    assert planning.audit_global_visual_plan(parsed) == []


def test_global_visual_plan_audit_rejects_monotonous_video_language():
    plan = [
        planning.GlobalVisualPlanItem(
            scene_index=index,
            narrative_role="Объяснение",
            visual_family="observational_broll",
            location="Серверная",
            primary_subject="Мониторы и стойки",
            shot_progression="Общий план серверов, монитор, терминал",
            palette="Синий LED",
            transition="Склейка",
            avoid="Повтор",
        )
        for index in range(1, 11)
    ]

    violations = planning.audit_global_visual_plan(plan)

    assert any("визуальных семейств" in item for item in violations)
    assert any("серверные/дата-центры" in item for item in violations)
    assert any("экраны/терминалы" in item for item in violations)


def test_global_visual_plan_audit_rejects_generic_repeated_narrative_role():
    plan = _global_plan(
        [
            Scene(
                index=index,
                start=f"00:00:{(index - 1) * 5:02d}",
                end=f"00:00:{index * 5:02d}",
                title=f"Scene {index}",
                voice_text=f"Narration {index}",
                visual_direction="",
                screen_text="",
                music_note="",
                retention_note="",
            )
            for index in range(1, 7)
        ]
    )
    plan = [
        planning.GlobalVisualPlanItem(
            **{
                **item.as_dict(),
                "narrative_role": "Развитие аргумента и продвижение истории",
            }
        )
        for item in plan
    ]

    violations = planning.audit_global_visual_plan(plan)

    assert any("narrative_role" in item for item in violations)


def test_final_shot_audit_rejects_cliche_returning_across_the_film():
    scenes = [
        Scene(
            index=index,
            start=f"00:00:{(index - 1) * 5:02d}",
            end=f"00:00:{index * 5:02d}",
            title=f"Scene {index}",
            voice_text="Narration",
            visual_direction="",
            screen_text="",
            music_note="",
            retention_note="",
        )
        for index in range(1, 9)
    ]
    shots = [
        _shot(
            index=index,
            scene_index=index,
            start=scenes[index - 1].start,
            end=scenes[index - 1].end,
            description="Generic glowing server rack",
            asset_query="server rack blinking lights",
        )
        for index in range(1, 9)
    ]

    violations = planning.audit_final_shot_list(shots, scenes)

    assert any("серверные/дата-центры" in item for item in violations)
    assert planning.audit_final_shot_list(
        shots,
        scenes,
        include_creative=False,
    ) == []
    assert any(
        "серверные/дата-центры" in item
        for item in planning.audit_final_visual_variety(shots, scenes)
    )


def test_pipeline_logs_creative_warning_without_discarding_shots(monkeypatch):
    _stub_global_plan(monkeypatch)
    logs: list[str] = []
    monkeypatch.setattr(
        planning,
        "request_lm_shots",
        lambda *_args, **_kwargs: [
            _raw_shot(
                description="Hands inspect a freshly printed sheet",
                asset_query="quality inspector printed sheet",
                image_prompt="Hands inspect a freshly printed sheet under neutral light.",
                video_prompt="Hands turn the sheet while the camera moves closer.",
                sfx_query="paper rustle",
            )
        ],
    )
    monkeypatch.setattr(
        planning,
        "audit_final_visual_variety",
        lambda _shots, _scenes: ["экраны/терминалы возвращаются в 14 сценах; максимум 12"],
    )

    shots = planning.call_lm_studio_for_shots(
        "http://lm.test/v1",
        "model",
        [_scene()],
        segments=[TranscriptSegment("00:00:00", "00:00:10", "Printed sheets.")],
        reference_density="off",
        log_callback=logs.append,
    )

    assert shots
    assert any(
        "Это не ломает таймлайн: шот-лист сохранён" in message
        for message in logs
    )


def test_global_visual_plan_parser_normalizes_harmless_local_model_shapes():
    scene = _scene()
    item = _global_plan([scene])[0].as_dict()
    item["narrative_role"] = ["Вводит тему", "создаёт вопрос"]
    item["visual_family"] = "industrial_location"
    item["avoid"] = ["серверная", "синие мониторы"]

    parsed = planning.parse_global_visual_plan(
        json.dumps([item], ensure_ascii=False),
        [scene],
    )

    assert parsed[0].narrative_role == "Вводит тему; создаёт вопрос"
    assert parsed[0].visual_family == "institutional_location"
    assert parsed[0].avoid == "серверная; синие мониторы"


def test_numeric_screen_text_gate_keeps_one_supported_fact_per_scene():
    scene = _scene()
    scene.voice_text = "In 2008, 95% of the volume replaced old notes."
    shots = [
        _shot(index=1, screen_text="2008: the crisis begins"),
        _shot(index=2, screen_text="95% replaced old notes"),
        _shot(index=3, screen_text="A system under pressure"),
        _shot(index=4, screen_text="2009: recovery"),
    ]

    planning.enforce_numeric_screen_text(shots, [scene])

    assert [shot.screen_text for shot in shots] == [
        "2008: the crisis begins",
        "",
        "",
        "",
    ]


@pytest.mark.parametrize("bad", ["{}", "not json", "[1, 2]"])
def test_request_rejects_non_shot_arrays(monkeypatch, bad):
    monkeypatch.setattr(planning.lm, "chat", lambda *_args, **_kwargs: bad)

    with pytest.raises(ValueError):
        planning.request_lm_shots("http://lm.test/v1", "model", "prompt")


def test_desktop_reexports_canonical_shot_planner():
    desktop = pytest.importorskip("content_pipeline_gui")

    assert desktop.call_lm_studio_for_shots is planning.call_lm_studio_for_shots
    assert desktop.build_heuristic_shots is planning.build_heuristic_shots
    assert desktop.retile_shots_contiguous is planning.retile_shots_contiguous
    assert desktop.normalize_shot_routes is planning.normalize_shot_routes
