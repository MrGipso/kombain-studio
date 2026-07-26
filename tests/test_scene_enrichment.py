from __future__ import annotations

import inspect
import json
import re

import pytest
from pydantic import ValidationError

from core import lm
from core import scenes
from core.models import Scene


def _scene(index: int, voice_text: str | None = None) -> Scene:
    return Scene(
        index=index,
        start=f"00:00:{index - 1:02d}",
        end=f"00:00:{index:02d}",
        title=f"Original title {index}",
        voice_text=voice_text or f"English narration for scene {index} with enough words.",
        visual_direction=f"Original visual {index}",
        screen_text=f"Original screen {index}",
        music_note=f"Original music {index}",
        retention_note=f"Original retention {index}",
    )


def _label(
    index: int,
    *,
    title: str | None = None,
    screen_text: str | None = None,
) -> scenes.SceneEnrichmentLabel:
    return scenes.SceneEnrichmentLabel(
        index=index,
        title=title or f"Updated title {index}",
        visual_direction=f"Updated concrete visual {index}",
        screen_text=(
            f"Updated screen {index}" if screen_text is None else screen_text
        ),
        music_note=f"Updated music {index}",
        retention_note=f"Updated retention {index}",
    )


def _reply(indices: list[int]) -> lm.StructuredReply[scenes.SceneEnrichmentEnvelope]:
    return lm.StructuredReply(
        value=scenes.SceneEnrichmentEnvelope(scenes=[_label(index) for index in indices]),
        attempts=1,
        transport="openai",
    )


def _batch_indices(messages: list[dict]) -> list[int]:
    prompt = messages[-1]["content"]
    compact = prompt.rsplit("Сцены (index|start-end|текст):\n", 1)[1]
    return [int(value) for value in re.findall(r"(?m)^(\d+)\|", compact)]


def _creative_snapshot(scene: Scene) -> tuple[str, str, str, str, str]:
    return (
        scene.title,
        scene.visual_direction,
        scene.screen_text,
        scene.music_note,
        scene.retention_note,
    )


def test_public_signature_is_preserved() -> None:
    assert list(inspect.signature(scenes.enrich_scenes_with_lm).parameters) == [
        "scenes",
        "base_url",
        "model",
        "ttl_seconds",
        "style_anchor",
        "character_description",
        "niche",
        "video_style",
        "log_callback",
        "require_all",
        "allow_plain_json_fallback",
    ]


def test_strict_envelope_rejects_coercion_extra_fields_and_oversized_batch() -> None:
    valid = {
        "title": "Title",
        "visual_direction": "A concrete physical scene",
        "screen_text": "Screen",
        "music_note": "Music",
        "retention_note": "Retention",
    }
    with pytest.raises(ValidationError):
        scenes.SceneEnrichmentEnvelope.model_validate(
            {"scenes": [{"index": "1", **valid}]}
        )
    with pytest.raises(ValidationError):
        scenes.SceneEnrichmentEnvelope.model_validate(
            {"scenes": [{"index": 1, **valid, "voice_text": "forbidden"}]}
        )
    with pytest.raises(ValidationError):
        scenes.SceneEnrichmentEnvelope.model_validate(
            {"scenes": [{"index": index, **valid} for index in range(1, 10)]}
        )
    with pytest.raises(ValidationError):
        scenes.SceneEnrichmentEnvelope.model_validate(
            {"scenes": [{"index": 1, **{**valid, "title": "   "}}]}
        )
    # The transport schema keeps a sanity ceiling, but a merely over-long
    # caption is no longer fatal: editorial limits are enforced when the batch
    # is applied, so one wordy field cannot discard seven good scenes.
    scenes.SceneEnrichmentEnvelope.model_validate(
        {"scenes": [{"index": 1, **{**valid, "screen_text": "x" * 101}}]}
    )
    with pytest.raises(ValidationError):
        scenes.SceneEnrichmentEnvelope.model_validate(
            {"scenes": [{"index": 1, **{**valid, "screen_text": "x" * 401}}]}
        )


def test_oversized_fields_are_clamped_instead_of_dropping_the_batch() -> None:
    """An over-long caption blanks one field, it does not lose the batch.

    This is the failure seen on a real 45-scene run: a 100+ character
    screen_text raised, the whole eight-scene batch was discarded, and scene
    analysis fell through to a worse segmentation strategy.
    """

    batch = [
        Scene(
            index=1,
            start="00:00:00",
            end="00:00:10",
            title="Исходный заголовок",
            voice_text="Здесь звучит цифра 42 процента в тексте диктора.",
            visual_direction="",
            screen_text="",
            music_note="",
            retention_note="",
        )
    ]
    label = scenes.SceneEnrichmentLabel(
        index=1,
        title="Новый заголовок",
        visual_direction="Конкретная физическая сцена",
        screen_text="слишком длинная надпись " * 12,
        music_note="Музыка",
        retention_note="Удержание",
    )

    assert scenes._apply_scene_enrichment_batch(batch, [label]) is True
    assert batch[0].title == "Новый заголовок"
    assert batch[0].screen_text == ""
    assert batch[0].start == "00:00:00"
    assert batch[0].end == "00:00:10"


def test_structured_enrichment_preserves_boundaries_and_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _scene(1)
    immutable = (original.index, original.start, original.end, original.voice_text)
    seen: dict = {}

    def fake_chat(settings, messages, response_model, **kwargs):
        seen.update(
            settings=settings,
            messages=messages,
            response_model=response_model,
            kwargs=kwargs,
        )
        return _reply([1])

    monkeypatch.setattr(scenes.lm_client, "chat_structured", fake_chat)

    result = scenes.enrich_scenes_with_lm(
        [original],
        "http://lm.example/v1/",
        "scene-model",
        ttl_seconds=75,
    )

    assert result == [original]
    assert (original.index, original.start, original.end, original.voice_text) == immutable
    assert _creative_snapshot(original) == (
        "Updated title 1",
        "Updated concrete visual 1",
        "Updated screen 1",
        "Updated music 1",
        "Updated retention 1",
    )
    assert seen["response_model"] is scenes.SceneEnrichmentEnvelope
    assert seen["settings"].lm_url == "http://lm.example/v1/"
    assert seen["kwargs"]["transport"] == "openai"
    assert seen["kwargs"]["model"] == "scene-model"
    assert seen["kwargs"]["ttl"] == 75
    assert seen["kwargs"]["timeout"] == 240
    assert "Не возвращай start, end или voice_text" in seen["messages"][1]["content"]


@pytest.mark.parametrize(
    "returned_indices",
    [
        [1],
        [1, 1],
        [1, 3],
    ],
    ids=["missing", "duplicate", "unknown"],
)
def test_non_exact_indices_leave_the_entire_batch_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    returned_indices: list[int],
) -> None:
    batch = [_scene(1), _scene(2)]
    before = [_creative_snapshot(scene) for scene in batch]

    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: _reply(returned_indices),
    )

    assert scenes.enrich_scenes_with_lm(batch, "http://lm/v1", "model") == batch
    assert [_creative_snapshot(scene) for scene in batch] == before


def test_reordered_exact_indices_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = [_scene(1), _scene(2)]
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: _reply([2, 1]),
    )

    scenes.enrich_scenes_with_lm(batch, "http://lm/v1", "model")

    assert [scene.title for scene in batch] == ["Updated title 1", "Updated title 2"]


def test_batches_are_bounded_by_scene_count(monkeypatch: pytest.MonkeyPatch) -> None:
    source = [_scene(index) for index in range(1, 18)]
    calls: list[list[int]] = []

    def fake_chat(_settings, messages, _response_model, **_kwargs):
        indices = _batch_indices(messages)
        calls.append(indices)
        return _reply(indices)

    monkeypatch.setattr(scenes.lm_client, "chat_structured", fake_chat)

    scenes.enrich_scenes_with_lm(source, "http://lm/v1", "model")

    assert [len(indices) for indices in calls] == [8, 8, 1]
    assert [index for call in calls for index in call] == list(range(1, 18))


def test_batches_are_bounded_by_voice_character_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = [_scene(index, chr(96 + index) * 5_000) for index in range(1, 6)]
    by_index = {scene.index: len(scene.voice_text) for scene in source}
    calls: list[list[int]] = []

    def fake_chat(_settings, messages, _response_model, **_kwargs):
        indices = _batch_indices(messages)
        calls.append(indices)
        return _reply(indices)

    monkeypatch.setattr(scenes.lm_client, "chat_structured", fake_chat)

    scenes.enrich_scenes_with_lm(source, "http://lm/v1", "model")

    assert [len(indices) for indices in calls] == [2, 2, 1]
    assert all(sum(by_index[index] for index in call) <= 12_000 for call in calls)


def test_oversized_scene_falls_back_without_blocking_later_scenes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = _scene(1, "a" * 12_001)
    later = _scene(2)
    oversized_before = _creative_snapshot(oversized)
    calls: list[list[int]] = []

    def fake_chat(_settings, messages, _response_model, **_kwargs):
        indices = _batch_indices(messages)
        calls.append(indices)
        return _reply(indices)

    monkeypatch.setattr(scenes.lm_client, "chat_structured", fake_chat)

    scenes.enrich_scenes_with_lm([oversized, later], "http://lm/v1", "model")

    assert calls == [[2]]
    assert _creative_snapshot(oversized) == oversized_before
    assert later.title == "Updated title 2"


@pytest.mark.parametrize(
    "failure",
    [
        lm.StructuredOutputError("invalid scene schema"),
        ConnectionError("LM transport offline"),
    ],
    ids=["schema", "transport"],
)
def test_failed_middle_batch_is_atomic_and_later_batch_continues(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    source = [_scene(index) for index in range(1, 18)]
    before = [_creative_snapshot(scene) for scene in source]
    call_number = 0

    def fake_chat(_settings, messages, _response_model, **_kwargs):
        nonlocal call_number
        call_number += 1
        indices = _batch_indices(messages)
        if call_number == 2:
            raise failure
        return _reply(indices)

    monkeypatch.setattr(scenes.lm_client, "chat_structured", fake_chat)

    result = scenes.enrich_scenes_with_lm(source, "http://lm/v1", "model")

    assert result == source
    assert all(source[index - 1].title == f"Updated title {index}" for index in range(1, 9))
    assert [
        _creative_snapshot(source[index - 1]) for index in range(9, 17)
    ] == before[8:16]
    assert source[16].title == "Updated title 17"


def test_transport_error_keeps_batch_unchanged_and_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = [_scene(1), _scene(2)]
    before = [_creative_snapshot(scene) for scene in source]
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("offline")),
    )

    assert scenes.enrich_scenes_with_lm(source, "http://lm/v1", "model") == source
    assert [_creative_snapshot(scene) for scene in source] == before


def test_required_enrichment_retries_in_plain_json_through_lm_studio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _scene(1)
    logs: list[str] = []
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("failed to parse grammar")
        ),
    )
    monkeypatch.setattr(
        scenes.lm_client,
        "chat",
        lambda *_args, **_kwargs: json.dumps(
            {"scenes": [_label(1).model_dump()]},
            ensure_ascii=False,
        ),
    )

    scenes.enrich_scenes_with_lm(
        [scene],
        "http://lm/v1",
        "model",
        log_callback=logs.append,
        require_all=True,
        allow_plain_json_fallback=True,
    )

    assert scene.title == "Updated title 1"
    assert any("обычным JSON" in message for message in logs)
    assert any("успешно проанализированы" in message for message in logs)


def test_plain_json_fallback_retries_one_malformed_scene_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _scene(1)
    logs: list[str] = []
    calls = 0
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("failed to parse grammar")
        ),
    )

    def plain_reply(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return '{"index": 1, "title" "broken"}'
        return json.dumps(_label(1).model_dump(), ensure_ascii=False)

    monkeypatch.setattr(scenes.lm_client, "chat", plain_reply)

    scenes.enrich_scenes_with_lm(
        [scene],
        "http://lm/v1",
        "model",
        log_callback=logs.append,
        require_all=True,
        allow_plain_json_fallback=True,
    )

    assert calls == 2
    assert scene.title == "Updated title 1"
    assert any("повреждённый JSON" in message for message in logs)


def test_required_enrichment_fails_instead_of_saving_unanalysed_scenes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _scene(1)
    before = _creative_snapshot(scene)
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("failed to parse grammar")
        ),
    )
    monkeypatch.setattr(
        scenes.lm_client,
        "chat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConnectionError("LM Studio offline")
        ),
    )

    with pytest.raises(RuntimeError, match="LM Studio не проанализировала"):
        scenes.enrich_scenes_with_lm(
            [scene],
            "http://lm/v1",
            "model",
            require_all=True,
            allow_plain_json_fallback=True,
        )

    assert _creative_snapshot(scene) == before


def test_plain_json_fallback_bounds_verbose_lm_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _scene(
        1,
        "Это достаточно длинный русский текст о работе защищённой печатной фабрики.",
    )
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("failed to parse grammar")
        ),
    )
    verbose_screen = (
        "За закрытыми воротами одного из самых охраняемых объектов страны "
        "непрерывно работают огромные печатные машины и системы контроля"
    )
    monkeypatch.setattr(
        scenes.lm_client,
        "chat",
        lambda *_args, **_kwargs: json.dumps(
            {
                "index": 99,
                "title": "Короткое название",
                "visual_direction": "Реальный печатный цех и работающие станки.",
                "screen_text": verbose_screen,
                "music_note": "Ровный промышленный гул.",
                "retention_note": "Показать масштаб производства.",
                "ignored_extra": "не должно попасть в контракт",
            },
            ensure_ascii=False,
        ),
    )

    scenes.enrich_scenes_with_lm(
        [scene],
        "http://lm/v1",
        "model",
        require_all=True,
        allow_plain_json_fallback=True,
    )

    assert scene.index == 1
    assert scene.screen_text == ""


def test_cyrillic_voice_falls_back_from_obvious_latin_title_and_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _scene(
        1,
        "Это достаточно длинный русский текст для точного определения письменности.",
    )
    before_title = scene.title
    envelope = scenes.SceneEnrichmentEnvelope(
        scenes=[
            _label(
                1,
                title="Completely translated English title",
                screen_text="Translated screen caption",
            )
        ]
    )
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: lm.StructuredReply(envelope, 1, "openai"),
    )

    scenes.enrich_scenes_with_lm([scene], "http://lm/v1", "model")

    assert scene.title == before_title
    assert scene.screen_text == ""
    assert scene.visual_direction == "Updated concrete visual 1"
    assert scene.music_note == "Updated music 1"


def test_latin_voice_falls_back_from_obvious_cyrillic_title_and_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _scene(1)
    before_title = scene.title
    envelope = scenes.SceneEnrichmentEnvelope(
        scenes=[
            _label(
                1,
                title="Полностью переведённый заголовок",
                screen_text="Русская экранная подпись",
            )
        ]
    )
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: lm.StructuredReply(envelope, 1, "openai"),
    )

    scenes.enrich_scenes_with_lm([scene], "http://lm/v1", "model")

    assert scene.title == before_title
    assert scene.screen_text == ""
    assert scene.visual_direction == "Updated concrete visual 1"


@pytest.mark.parametrize(
    ("title", "screen_text"),
    [
        ("GPU", "RTX 5090"),
        ("GPU сервер", "2026"),
        ("ИИ", "42"),
    ],
)
def test_numbers_acronyms_and_short_mixed_labels_are_allowed(
    monkeypatch: pytest.MonkeyPatch,
    title: str,
    screen_text: str,
) -> None:
    scene = _scene(
        1,
        "Это достаточно длинный русский текст с числами 5090, 2026 и 42.",
    )
    envelope = scenes.SceneEnrichmentEnvelope(
        scenes=[_label(1, title=title, screen_text=screen_text)]
    )
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: lm.StructuredReply(envelope, 1, "openai"),
    )

    scenes.enrich_scenes_with_lm([scene], "http://lm/v1", "model")

    assert (scene.title, scene.screen_text) == (title, screen_text)


def test_screen_text_requires_a_number_supported_by_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _scene(1, "В 2008 году финансовая система оказалась под угрозой.")
    envelope = scenes.SceneEnrichmentEnvelope(
        scenes=[_label(1, screen_text="Кризис финансовой системы")]
    )
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: lm.StructuredReply(envelope, 1, "openai"),
    )

    scenes.enrich_scenes_with_lm([scene], "http://lm/v1", "model")

    assert scene.screen_text == ""


def test_screen_text_rejects_a_number_not_present_in_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _scene(1, "В 2008 году финансовая система оказалась под угрозой.")
    envelope = scenes.SceneEnrichmentEnvelope(
        scenes=[_label(1, screen_text="Кризис 2009 года")]
    )
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: lm.StructuredReply(envelope, 1, "openai"),
    )

    scenes.enrich_scenes_with_lm([scene], "http://lm/v1", "model")

    assert scene.screen_text == ""
