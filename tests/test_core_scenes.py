from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import scenes
from core.models import Scene, TranscriptSegment, seconds_to_timecode, timecode_to_seconds


def _segments(count: int = 4, seconds: int = 20) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(
            start=seconds_to_timecode(index * seconds),
            end=seconds_to_timecode((index + 1) * seconds),
            text=f"Topic {index + 1} has enough concrete words.",
        )
        for index in range(count)
    ]


def _response(content) -> object:
    payload = {
        "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    return Response()


def test_duration_fallback_is_deterministic(monkeypatch: pytest.MonkeyPatch):
    segments = _segments()

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("embedding server offline")

    monkeypatch.setattr(scenes, "_lm_embeddings", unavailable)
    expected = scenes.build_scenes_from_segments(segments)

    first = scenes.build_scenes_by_embedding(segments, "http://lm/v1")
    second = scenes.build_scenes_by_embedding(segments, "http://lm/v1")

    assert first == second == expected
    assert [(scene.start, scene.end) for scene in first] == [
        ("00:00:00", "00:01:00"),
        ("00:01:00", "00:01:20"),
    ]


def test_embedding_boundaries_are_repeatable(monkeypatch: pytest.MonkeyPatch):
    segments = _segments(seconds=15)
    vectors = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0]]
    monkeypatch.setattr(scenes, "_lm_embeddings", lambda *_args, **_kwargs: vectors)
    logs: list[str] = []

    first = scenes.build_scenes_by_embedding(
        segments,
        "http://lm/v1",
        target_scenes=2,
        min_seconds=1,
        max_seconds=120,
        min_unit_words=1,
        window=1,
        log_callback=logs.append,
    )
    second = scenes.build_scenes_by_embedding(
        segments,
        "http://lm/v1",
        target_scenes=2,
        min_seconds=1,
        max_seconds=120,
        min_unit_words=1,
        window=1,
    )

    assert first == second
    assert [scene.voice_text for scene in first] == [
        "Topic 1 has enough concrete words. Topic 2 has enough concrete words.",
        "Topic 3 has enough concrete words. Topic 4 has enough concrete words.",
    ]
    assert logs == [
        "TextTiling: реплик=4, границ=1, сцен=2 (sensitivity=0.6, target=2, окно=1)"
    ]


def test_embedding_target_one_produces_one_scene_when_duration_allows(
    monkeypatch: pytest.MonkeyPatch,
):
    segments = _segments(count=4, seconds=10)
    monkeypatch.setattr(
        scenes,
        "_lm_embeddings",
        lambda *_args, **_kwargs: [[1.0, float(index)] for index in range(4)],
    )

    result = scenes.build_scenes_by_embedding(
        segments,
        "http://lm/v1",
        target_scenes=1,
        min_seconds=1,
        max_seconds=120,
        min_unit_words=1,
    )

    assert len(result) == 1
    assert result[0].start == segments[0].start
    assert result[0].end == segments[-1].end
    assert result[0].voice_text == " ".join(segment.text for segment in segments)


def test_embedding_partition_respects_minimum_after_maximum_splitting(
    monkeypatch: pytest.MonkeyPatch,
):
    segments = _segments(count=5, seconds=10)
    monkeypatch.setattr(
        scenes,
        "_lm_embeddings",
        lambda *_args, **_kwargs: [
            [1.0, 0.0],
            [0.9, 0.1],
            [0.0, 1.0],
            [0.1, 0.9],
            [1.0, 0.0],
        ],
    )

    result = scenes.build_scenes_by_embedding(
        segments,
        "http://lm/v1",
        target_scenes=2,
        min_seconds=20,
        max_seconds=30,
        min_unit_words=1,
        window=1,
    )

    durations = [
        timecode_to_seconds(scene.end) - timecode_to_seconds(scene.start)
        for scene in result
    ]
    assert len(result) == 2
    assert all(20 <= duration <= 30 for duration in durations)
    assert " ".join(scene.voice_text for scene in result) == " ".join(
        segment.text for segment in segments
    )


def test_scene_partition_validator_rejects_subsecond_overlap():
    segments = [
        TranscriptSegment("00:00:00.000", "00:00:05.900", "First"),
        TranscriptSegment("00:00:05.100", "00:00:10.000", "Second"),
    ]
    partition = [
        scenes.make_scene_from_segments(1, [segments[0]]),
        scenes.make_scene_from_segments(2, [segments[1]]),
    ]

    assert scenes._precise_timecode_seconds("00:00:05,900") == pytest.approx(5.9)
    assert scenes._validate_scene_partition(partition, segments) is False


def test_embedding_client_sorts_vectors_and_applies_ttl(
    monkeypatch: pytest.MonkeyPatch,
):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                }
            ).encode("utf-8")

    def urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(scenes.lm_client.urllib.request, "urlopen", urlopen)

    result = scenes._lm_embeddings(
        "http://lm/v1/", "embed-model", ["first", "second"], ttl_seconds=30
    )

    assert result == [[1.0, 0.0], [0.0, 1.0]]
    assert seen["request"].full_url == "http://lm/v1/embeddings"
    assert json.loads(seen["request"].data) == {
        "model": "embed-model",
        "input": ["first", "second"],
        "ttl": 30,
    }
    assert seen["timeout"] == 120


@pytest.mark.parametrize(
    ("items", "message"),
    [
        (
            [
                {"index": 0, "embedding": [1.0, 0.0]},
                {"index": 0, "embedding": [0.0, 1.0]},
            ],
            "indexes mismatch",
        ),
        (
            [
                {"index": 0, "embedding": [1.0, 0.0]},
                {"index": 1, "embedding": [1.0]},
            ],
            "dimension mismatch",
        ),
        (
            [
                {"index": 0, "embedding": [1.0, float("nan")]},
                {"index": 1, "embedding": [0.0, 1.0]},
            ],
            "non-finite",
        ),
        (
            [
                {"index": 0, "embedding": []},
                {"index": 1, "embedding": [0.0, 1.0]},
            ],
            "non-empty",
        ),
    ],
)
def test_embedding_client_rejects_malformed_vectors_before_segmentation(
    monkeypatch: pytest.MonkeyPatch,
    items: list[dict],
    message: str,
):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"data": items}).encode("utf-8")

    monkeypatch.setattr(
        scenes.lm_client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )

    with pytest.raises(ValueError, match=message):
        scenes._lm_embeddings("http://lm/v1", "embed-model", ["one", "two"])


def test_lm_array_parser_accepts_fences_prose_and_repairs_complete_prefix():
    assert scenes.parse_lm_json_array(
        '```json\n[{"index": 1}]\n``` trailing prose'
    ) == [{"index": 1}]
    assert scenes.parse_lm_json_array(
        'answer: [{"index": 1}, {"index": 2}'
    ) == [{"index": 1}, {"index": 2}]

    with pytest.raises(ValueError, match="не вернул JSON-массив"):
        scenes.parse_lm_json_array('{"index": 1}', "scene parser")


def test_lm_segment_parsing_keeps_source_text_and_fills_uncovered_tail(
    monkeypatch: pytest.MonkeyPatch,
):
    segments = [
        TranscriptSegment("00:00:00", "00:00:10", "Alpha one"),
        TranscriptSegment("00:00:10", "00:00:20", "Alpha two"),
        TranscriptSegment("00:00:20", "00:00:30", "Beta three"),
        TranscriptSegment("00:00:30", "00:00:40", "Gamma four"),
    ]
    raw = [
        {
            "title": "Ocean chapter",
            "segment_ids": [1, 2],
            "purpose": "hook",
            "visual_direction": "Wide ocean panorama",
        },
        {
            "title": "Server chapter",
            "segment_ids": [2, "bad", 3],
            "retention_note": "turn",
            "visual_direction": "Macro server detail",
        },
    ]
    seen = {}

    def urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return _response(raw)

    monkeypatch.setattr(scenes.lm_client.urllib.request, "urlopen", urlopen)

    result = scenes.call_lm_studio_for_segments(
        "http://lm/v1/", "scene-model", segments, ttl_seconds=90
    )

    assert [scene.index for scene in result] == [1, 2, 3]
    assert [scene.voice_text for scene in result] == [
        "Alpha one Alpha two",
        "Beta three",
        "Gamma four",
    ]
    assert [(scene.start, scene.end) for scene in result] == [
        ("00:00:00", "00:00:20"),
        ("00:00:20", "00:00:30"),
        ("00:00:30", "00:00:40"),
    ]
    assert result[0].retention_note == "hook"
    assert result[1].retention_note == "turn"
    assert seen["request"].full_url == "http://lm/v1/chat/completions"
    assert json.loads(seen["request"].data)["ttl"] == 90
    assert seen["timeout"] == 240


def test_lm_segment_parsing_repairs_initial_and_middle_gaps(
    monkeypatch: pytest.MonkeyPatch,
):
    segments = _segments(count=5, seconds=10)
    raw = [
        {
            "title": "Second scene",
            "segment_ids": [2],
            "visual_direction": "A red tram crosses a snowy square",
        },
        {
            "title": "Fourth scene",
            "segment_ids": [4],
            "visual_direction": "A blue boat enters a quiet harbour",
        },
    ]
    monkeypatch.setattr(
        scenes.lm_client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _response(raw),
    )

    result = scenes.call_lm_studio_for_segments(
        "http://lm/v1", "scene-model", segments
    )

    assert [scene.voice_text for scene in result] == [
        segment.text for segment in segments
    ]
    assert result[0].start == segments[0].start
    assert result[-1].end == segments[-1].end
    assert [scene.index for scene in result] == list(range(1, len(result) + 1))


def test_lm_segment_parsing_repairs_duplicates_and_out_of_order_ids(
    monkeypatch: pytest.MonkeyPatch,
):
    segments = _segments(count=4, seconds=10)
    # Both the scene objects and ids inside the first object are out of order;
    # duplicated ids must not duplicate narration in the result.
    raw = [
        {
            "title": "Middle scene",
            "segment_ids": [3, 2, 2],
            "visual_direction": "Macro view of a green circuit board",
        },
        {
            "title": "Opening scene",
            "segment_ids": [1, 1],
            "visual_direction": "Wide sunrise over a mountain ridge",
        },
    ]
    monkeypatch.setattr(
        scenes.lm_client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _response(raw),
    )

    result = scenes.call_lm_studio_for_segments(
        "http://lm/v1", "scene-model", segments
    )

    assert [scene.voice_text for scene in result] == [
        segments[0].text,
        f"{segments[1].text} {segments[2].text}",
        segments[3].text,
    ]
    flattened = " ".join(scene.voice_text for scene in result)
    assert flattened == " ".join(segment.text for segment in segments)
    assert [scene.index for scene in result] == [1, 2, 3]


def test_lm_segment_parsing_rejects_sparse_range_with_distant_id(
    monkeypatch: pytest.MonkeyPatch,
):
    segments = _segments(count=6, seconds=10)
    raw = [
        {
            "title": "Hallucinated giant range",
            "segment_ids": [1, 6],
            "visual_direction": "One implausibly broad scene",
        },
        {
            "title": "Valid middle",
            "segment_ids": [2, 3],
            "visual_direction": "A focused two-part scene",
        },
    ]
    monkeypatch.setattr(
        scenes.lm_client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _response(raw),
    )

    result = scenes.call_lm_studio_for_segments(
        "http://lm/v1", "scene-model", segments,
    )

    assert "Hallucinated giant range" not in [scene.title for scene in result]
    assert " ".join(scene.voice_text for scene in result) == " ".join(
        segment.text for segment in segments
    )
    assert any(scene.voice_text == f"{segments[1].text} {segments[2].text}" for scene in result)


def test_enrichment_changes_labels_but_not_boundaries_or_voice(
    monkeypatch: pytest.MonkeyPatch,
):
    original = Scene(
        index=1,
        start="00:00:03",
        end="00:00:19",
        title="Old title",
        voice_text="Exact narration",
        visual_direction="Old visual",
        screen_text="Old screen",
        music_note="Old music",
        retention_note="Old retention",
    )
    envelope = scenes.SceneEnrichmentEnvelope(
        scenes=[
            scenes.SceneEnrichmentLabel(
                index=1,
                title="New title",
                visual_direction="New concrete visual",
                screen_text="",
                music_note="New music",
                retention_note="New retention",
            )
        ]
    )
    monkeypatch.setattr(
        scenes.lm_client,
        "chat_structured",
        lambda *_args, **_kwargs: scenes.lm_client.StructuredReply(
            value=envelope,
            attempts=1,
            transport="openai",
        ),
    )

    result = scenes.enrich_scenes_with_lm(
        [original], "http://lm/v1", "scene-model"
    )

    assert result == [original]
    assert (original.start, original.end, original.voice_text) == (
        "00:00:03",
        "00:00:19",
        "Exact narration",
    )
    assert (
        original.title,
        original.visual_direction,
        original.screen_text,
        original.music_note,
        original.retention_note,
    ) == (
        "New title",
        "New concrete visual",
        "",
        "New music",
        "New retention",
    )


def test_script_scene_analysis_preserves_transport_and_builds_timeline(
    monkeypatch: pytest.MonkeyPatch,
):
    raw = [
        {
            "title": "Opening",
            "voice_text": "A short opening line.",
            "visual_direction": "Concrete wide shot",
            "screen_text": "OPEN",
            "music_note": "calm",
            "retention_note": "hook",
        },
        {
            "title": "Second",
            "voice_text": "A second short line.",
            "visual_direction": "Concrete detail",
        },
    ]
    seen = {}

    def urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return _response(raw)

    monkeypatch.setattr(scenes.lm_client.urllib.request, "urlopen", urlopen)

    result = scenes.call_lm_studio(
        "http://lm/v1/",
        "scene-model",
        "# Heading\nA short opening line.\n\nA second short line.",
        ttl_seconds=45,
        video_style="documentary",
    )

    assert [scene.title for scene in result] == ["Opening", "Second"]
    assert result[0].start == "00:00:00"
    assert result[0].end == result[1].start
    payload = json.loads(seen["request"].data)
    assert seen["request"].full_url == "http://lm/v1/chat/completions"
    assert seen["timeout"] == 180
    assert payload["ttl"] == 45
    assert "# Heading" not in payload["messages"][1]["content"]


def test_core_scenes_imports_without_backend_or_tkinter():
    root = Path(__file__).resolve().parents[1]
    script = r"""
import builtins
import sys

real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "backend" or name.startswith("backend.") or name.startswith("tkinter"):
        raise AssertionError(f"core.scenes imported forbidden dependency: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded
import core.scenes
assert "tkinter" not in sys.modules
assert not any(name == "backend" or name.startswith("backend.") for name in sys.modules)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def _chapter_segments(count: int, seconds: int = 10) -> list[TranscriptSegment]:
    """A chapter whose first half and second half are about different things."""

    topics = ("harbour cranes lift steel containers onto the waiting cargo ship.",
              "central bank clerks reconcile ledgers inside a quiet stone hall.")
    return [
        TranscriptSegment(
            start=seconds_to_timecode(index * seconds),
            end=seconds_to_timecode((index + 1) * seconds),
            text=topics[0 if index < count // 2 else 1],
        )
        for index in range(count)
    ]


def test_embedding_partition_cuts_on_the_topic_shift(monkeypatch: pytest.MonkeyPatch):
    """Inside one chapter the cut follows meaning, not the stopwatch."""

    segments = _chapter_segments(8)

    def fake_embeddings(base_url, model, texts, timeout=120, ttl_seconds=None):
        # Two well-separated clusters matching the two halves of the chapter.
        return [
            [1.0, 0.0] if "harbour" in text else [0.0, 1.0]
            for text in texts
        ]

    monkeypatch.setattr(scenes, "_lm_embeddings", fake_embeddings)

    groups = scenes.partition_segments_by_embedding(
        segments,
        "http://lm/v1",
        min_seconds=10,
        max_seconds=90,
        target_seconds=40,
    )

    assert sum(len(group) for group in groups) == len(segments)
    assert [segment for group in groups for segment in group] == segments
    assert len(groups) > 1
    boundary_texts = {group[0].text for group in groups}
    assert len(boundary_texts) > 1


def test_embedding_partition_falls_back_when_endpoint_is_down(
    monkeypatch: pytest.MonkeyPatch,
):
    """No embedding endpoint must degrade to duration, never lose narration."""

    segments = _chapter_segments(8)
    notes: list[str] = []

    def broken(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(scenes, "_lm_embeddings", broken)

    groups = scenes.partition_segments_by_embedding(
        segments,
        "http://lm/v1",
        min_seconds=10,
        max_seconds=90,
        target_seconds=40,
        log_callback=notes.append,
    )
    expected = scenes.partition_segments_by_duration(
        segments, min_seconds=10, max_seconds=90, target_seconds=40
    )

    assert groups == expected
    assert sum(len(group) for group in groups) == len(segments)
    assert any("длительности" in note for note in notes)


def test_script_chapters_use_the_supplied_partitioner(tmp_path: Path):
    """Authored chapters stay authoritative; only the inner cuts are delegated."""

    from core import script_align

    sentences = [
        "Первая глава открывает историю печатного цеха и рассказывает про станки очень подробно",
        "Продолжение первой главы уточняет как именно устроена подача бумаги внутри большого станка",
        "Завершение первой главы описывает упаковку готовых листов в тяжёлые металлические ящики склада",
        "Вторая глава переходит к перевозке груза через океан на больших контейнерных судах компании",
        "Продолжение второй главы объясняет маршруты судов и расписание портовых кранов в дневную смену",
        "Финал второй главы показывает разгрузку контейнеров и оформление сопроводительных документов в порту",
        "Третья глава посвящена банковским залам где клерки сверяют бумажные реестры при дневном свете",
        "Продолжение третьей главы раскрывает как проверяются подписи и печати на старых архивных бумагах",
        "Последняя глава подводит итог и возвращает зрителя к началу истории про печатный цех",
    ]
    segments = [
        TranscriptSegment(
            start=seconds_to_timecode(index * 10),
            end=seconds_to_timecode((index + 1) * 10),
            text=text,
        )
        for index, text in enumerate(sentences)
    ]
    (tmp_path / "00_script.json").write_text(
        json.dumps(
            {
                "beats": [
                    {"title": f"Глава {number}", "text": " ".join(sentences[start:start + 3])}
                    for number, start in enumerate((0, 3, 6), start=1)
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    calls: list[int] = []

    def partitioner(segs, *, min_seconds, max_seconds, target_seconds):
        calls.append(len(segs))
        return [list(segs)]

    result = script_align.scenes_from_script(
        tmp_path, segments, Scene, 10, 90, 40, partitioner=partitioner
    )

    assert result is not None, "authored chapters should align to the narration"
    assert calls == [3, 3, 3], "each chapter is partitioned separately"
    assert len(result) == 3
    assert result[0].start == "00:00:00"
    assert result[-1].end == segments[-1].end


def test_script_chapters_recover_when_partitioner_loses_segments(tmp_path: Path):
    """A partitioner may degrade, but narration must never be dropped."""

    from core import script_align

    sentences = [
        f"Предложение номер {index} рассказывает свою отдельную мысль достаточно подробно и длинно"
        for index in range(9)
    ]
    segments = [
        TranscriptSegment(
            start=seconds_to_timecode(index * 10),
            end=seconds_to_timecode((index + 1) * 10),
            text=text,
        )
        for index, text in enumerate(sentences)
    ]
    (tmp_path / "00_script.json").write_text(
        json.dumps(
            {
                "beats": [
                    {"title": f"Глава {number}", "text": " ".join(sentences[start:start + 3])}
                    for number, start in enumerate((0, 3, 6), start=1)
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def lossy(segs, *, min_seconds, max_seconds, target_seconds):
        return [list(segs[:1])]  # silently drops the rest of the chapter

    result = script_align.scenes_from_script(
        tmp_path, segments, Scene, 10, 90, 40, partitioner=lossy
    )

    assert result is not None
    covered = sum(
        len([s for s in segments if scene.start <= s.start < scene.end])
        for scene in result
    )
    assert covered == len(segments)
