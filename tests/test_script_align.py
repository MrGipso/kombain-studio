from __future__ import annotations

import json

import pytest

from core.models import Scene, TranscriptSegment, timecode_to_seconds
from core.script_align import (
    align_beats_to_segments,
    load_script_beats,
    scenes_from_script,
)


def _segments(*texts: str) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(
            start=f"00:00:{index * 5:02d}",
            end=f"00:00:{(index + 1) * 5:02d}",
            text=text,
        )
        for index, text in enumerate(texts)
    ]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"beats": "not-a-list"},
        {"beats": [{"text": 123}, {"text": "valid"}, {"text": "also valid"}]},
    ],
)
def test_load_script_beats_treats_malformed_shapes_as_unusable(tmp_path, payload):
    (tmp_path / "00_script.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )

    assert load_script_beats(tmp_path) is None


def test_alignment_rejects_a_script_with_an_unrelated_first_beat():
    beats = [
        {"text": "Космический корабль садится на далёкую красную планету"},
        {"text": "Вторая глава объясняет устройство локальной нейросети"},
        {"text": "Третья глава подводит ясный практический итог"},
    ]
    segments = _segments(
        "Сегодня мы обсуждаем старую городскую архитектуру",
        "Начнём с устройства каменных арок и колонн",
        "Вторая глава объясняет устройство локальной нейросети",
        "Она работает прямо на компьютере автора",
        "Третья глава подводит ясный практический итог",
        "Теперь можно переходить к следующему этапу",
    )

    assert align_beats_to_segments(beats, segments) is None


def test_alignment_accepts_a_leading_hook_and_covers_every_segment_once():
    beats = [
        {"text": "Первый раздел показывает локальный рабочий процесс"},
        {"text": "Второй раздел объясняет смысловые границы сцен"},
        {"text": "Третий раздел завершает разбор практическим выводом"},
    ]
    segments = _segments(
        "Что если весь ролик можно собрать без облачных сервисов",
        "Первый раздел показывает локальный рабочий процесс",
        "Модель получает подготовленную транскрипцию",
        "Второй раздел объясняет смысловые границы сцен",
        "Каждая граница остаётся привязанной ко времени",
        "Третий раздел завершает разбор практическим выводом",
    )

    ranges = align_beats_to_segments(beats, segments)

    assert ranges is not None
    covered = [index for start, end in ranges for index in range(start, end)]
    assert covered == list(range(len(segments)))


def test_authored_beats_are_chapters_split_into_edit_sized_timed_scenes(tmp_path):
    anchors = {
        0: "Первая глава начинается с защищённого комплекса",
        24: "Вторая глава начинается с печатных машин",
        48: "Третья глава начинается с проверки банкнот",
    }

    def timestamp(seconds: int) -> str:
        return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"

    segments = [
        TranscriptSegment(
            start=timestamp(index * 5),
            end=timestamp((index + 1) * 5),
            text=anchors.get(index, f"Подробность номер {index} завершает предложение."),
        )
        for index in range(72)
    ]
    (tmp_path / "00_script.json").write_text(
        json.dumps(
            {
                "beats": [
                    {"title": "Комплекс", "text": anchors[0], "goal": "Ввести место"},
                    {"title": "Печать", "text": anchors[24], "goal": "Показать процесс"},
                    {"title": "Контроль", "text": anchors[48], "goal": "Показать проверку"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = scenes_from_script(
        tmp_path,
        segments,
        Scene,
        min_seconds=20,
        max_seconds=60,
        target_seconds=45,
    )

    assert result is not None
    assert len(result) > 3
    assert [scene.index for scene in result] == list(range(1, len(result) + 1))
    assert all(
        20 <= timecode_to_seconds(scene.end) - timecode_to_seconds(scene.start) <= 60
        for scene in result
    )
    assert result[0].start == segments[0].start
    assert result[-1].end == segments[-1].end
    assert " ".join(scene.voice_text for scene in result) == " ".join(
        segment.text for segment in segments
    )
    assert any(scene.start == segments[24].start for scene in result)
    assert any(scene.start == segments[48].start for scene in result)
    assert any("·" in scene.title for scene in result)
    assert all(
        current.end == following.start
        for current, following in zip(result, result[1:])
    )
