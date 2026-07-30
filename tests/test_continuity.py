from __future__ import annotations

import json

from core.continuity import (
    apply_storyboard_context,
    build_context_bible,
    compile_generation_prompt,
    load_context_bible,
    write_context_bible,
)
from core.models import Scene, Shot


def _scene(index: int = 1) -> Scene:
    return Scene(
        index=index,
        start="00:00:00",
        end="00:00:10",
        title="Получение сообщения",
        voice_text="Герой получает сообщение и понимает, что данные потеряны.",
        visual_direction="Кабинет, герой берёт телефон со стола.",
        screen_text="",
        music_note="",
        retention_note="",
    )


def _shot(index: int, description: str) -> Shot:
    return Shot(
        index=index,
        scene_index=1,
        start=f"00:00:0{(index - 1) * 5}",
        end=f"00:00:{index * 5:02d}",
        visual_type="ai_image_then_i2v",
        description=description,
        asset_query="man phone office",
        image_model="krea2_turbo",
        video_model="ltx2.3_i2v",
        image_prompt=f"A medium view of {description}.",
        video_prompt="The hand moves while the camera slowly pushes in.",
        screen_text="",
        motion="slow push",
        priority="medium",
        fallback="Ken Burns",
    )


def _plan() -> list[dict]:
    return [
        {
            "scene_index": 1,
            "story_thread": "Потеря данных",
            "continuity_mode": "continuous_action",
            "location": "Кабинет героя с тёмным деревянным столом",
            "location_id": "location-office-alex",
            "primary_subject": "Алекс",
            "subject_ids": "subject-alex",
            "entry_state": "Телефон лежит на столе",
            "scene_event": "Алекс берёт телефон и читает сообщение",
            "exit_state": "Телефон в руке, Алекс понимает проблему",
            "transition": "Стык по взгляду на экран",
        }
    ]


def test_context_defaults_create_one_causal_group_and_reference():
    shots = [
        _shot(1, "Алекс замечает телефон на столе"),
        _shot(2, "Алекс поднимает телефон и читает сообщение"),
    ]

    apply_storyboard_context(shots, _plan())

    assert shots[0].continuity_mode == "continuous_action"
    assert shots[0].continuity_group_id == shots[1].continuity_group_id
    assert shots[0].location_id == "location-office-alex"
    assert shots[0].subject_ids == ["subject-alex"]
    assert shots[0].state_before == "Телефон лежит на столе"
    assert shots[1].state_before == shots[0].state_after
    assert shots[1].state_after == "Телефон в руке, Алекс понимает проблему"
    assert shots[0].reference_frame_id == shots[1].reference_frame_id
    assert shots[0].purpose
    assert shots[1].change_from_previous


def test_continuous_action_reconciles_authored_boundary_state():
    shots = [
        _shot(1, "Алекс замечает телефон на столе"),
        _shot(2, "Алекс поднимает телефон и читает сообщение"),
    ]
    shots[0].state_after = "Телефон уже в правой руке"
    shots[1].state_before = "Телефон всё ещё лежит на столе"

    apply_storyboard_context(shots, _plan())

    assert shots[1].state_before == "Телефон уже в правой руке"


def test_context_bible_roundtrip_and_tamper_detection(tmp_path):
    scenes = [_scene()]
    shots = [_shot(1, "Алекс берёт телефон")]
    apply_storyboard_context(shots, _plan())
    bible = build_context_bible(
        scenes,
        _plan(),
        shots,
        character_description="Мужчина 40 лет, синяя рубашка",
    )

    path = write_context_bible(tmp_path, bible)
    loaded = load_context_bible(tmp_path)

    assert loaded is not None
    assert loaded["entities"][0]["id"] == "subject-alex"
    assert loaded["entities"][0]["appearance_anchor"].startswith("Мужчина")
    assert loaded["continuity_groups"][0]["shot_indices"] == [1]

    damaged = json.loads(path.read_text(encoding="utf-8"))
    damaged["shots"][0]["state_after"] = "Подменённое состояние"
    path.write_text(json.dumps(damaged, ensure_ascii=False), encoding="utf-8")
    assert load_context_bible(tmp_path) is None


def test_independent_broll_does_not_share_reference_or_group():
    plan = [{**_plan()[0], "continuity_mode": "independent"}]
    shots = [_shot(1, "Улица"), _shot(2, "Архивный документ")]

    apply_storyboard_context(shots, plan)

    assert shots[0].continuity_group_id != shots[1].continuity_group_id
    assert shots[0].reference_frame_id == ""
    assert shots[1].reference_frame_id == ""


def test_provider_prompt_compiler_uses_bible_descriptions_and_causal_state():
    shots = [_shot(1, "Алекс берёт телефон")]
    apply_storyboard_context(shots, _plan())
    bible = build_context_bible(
        [_scene()],
        _plan(),
        shots,
        character_description="Мужчина 40 лет в синей рубашке",
    )

    image_prompt = compile_generation_prompt(
        shots[0].image_prompt,
        shots[0],
        target="image",
        context_bible=bible,
    )
    video_prompt = compile_generation_prompt(
        shots[0].video_prompt,
        shots[0],
        target="video",
        context_bible=bible,
    )

    assert "Кабинет героя" in image_prompt
    assert "Мужчина 40 лет" in image_prompt
    assert "captured action: Алекс берёт телефон" in image_prompt
    assert "initial visible state: Телефон лежит на столе" in video_prompt
    assert "final visible state:" in video_prompt
    assert "preserve the identity" in video_prompt


def test_provider_prompt_compiler_keeps_legacy_prompt_unchanged():
    assert (
        compile_generation_prompt(
            "Plain legacy prompt",
            {},
            target="image",
        )
        == "Plain legacy prompt"
    )
