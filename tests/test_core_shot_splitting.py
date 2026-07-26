from __future__ import annotations

from core.models import Scene, Shot, TranscriptSegment, timecode_to_seconds
from core.shots import (
    collapse_redundant_shot_runs,
    enforce_minimum_shot_durations,
    split_shots_for_stock,
    split_stock_shots_only,
)


def _shot(index: int, start: str, end: str, visual_type: str = "ai_image_then_i2v") -> Shot:
    return Shot(
        index=index,
        scene_index=1,
        start=start,
        end=end,
        visual_type=visual_type,
        description="description",
        asset_query="english stock query",
        image_model="krea2_turbo",
        video_model="ltx2.3_i2v",
        image_prompt="image prompt",
        video_prompt="video prompt",
        screen_text="",
        motion="slow push",
        priority="medium",
        fallback="fallback",
    )


def _durations(shots: list[Shot]) -> list[int]:
    return [
        timecode_to_seconds(shot.end) - timecode_to_seconds(shot.start)
        for shot in shots
    ]


def test_equal_stock_cuts_are_bounded_contiguous_and_reindexed():
    original = _shot(7, "00:00:00", "00:00:15")

    result = split_shots_for_stock([original], 5, variable=False)

    assert _durations(result) == [5, 5, 5]
    assert [shot.index for shot in result] == [1, 2, 3]
    assert result[0].start == "00:00:00"
    assert result[-1].end == "00:00:15"
    assert all(shot.visual_type == "stock_video" for shot in result)
    assert all(not shot.image_model and not shot.video_model for shot in result)
    assert original.visual_type == "ai_image_then_i2v"


def test_variable_stock_cuts_are_deterministic_and_snap_to_phrase_end():
    shot = _shot(1, "00:00:00", "00:00:14")
    segments = [
        TranscriptSegment("00:00:00", "00:00:04", "one"),
        TranscriptSegment("00:00:04", "00:00:09", "two"),
    ]

    first = split_shots_for_stock([shot], 5, segments=segments, variable=True)
    second = split_shots_for_stock([shot], 5, segments=segments, variable=True)

    assert [(item.start, item.end) for item in first] == [
        (item.start, item.end) for item in second
    ]
    assert sum(_durations(first)) == 14
    assert all(1 <= duration <= 5 for duration in _durations(first))
    assert first[0].start == "00:00:00" and first[-1].end == "00:00:14"
    assert "00:00:04" in {item.end for item in first}


def test_hybrid_split_preserves_non_stock_slots_and_reindexes_all():
    shots = [
        _shot(1, "00:00:00", "00:00:12", "stock_video"),
        _shot(2, "00:00:12", "00:00:22", "ai_image_then_i2v"),
        _shot(3, "00:00:22", "00:00:27", "still_kenburns"),
    ]

    result = split_stock_shots_only(shots, 5, variable=False)

    assert [shot.index for shot in result] == [1, 2, 3, 4, 5]
    assert [shot.visual_type for shot in result] == [
        "stock_video",
        "stock_video",
        "stock_video",
        "ai_image_then_i2v",
        "still_kenburns",
    ]
    assert (result[3].start, result[3].end) == ("00:00:12", "00:00:22")
    assert (result[4].start, result[4].end) == ("00:00:22", "00:00:27")


def test_invalid_segment_snap_points_are_ignored():
    result = split_shots_for_stock(
        [_shot(1, "00:00:00", "00:00:07")],
        5,
        segments=[{}, object(), {"end": "bad"}],
        variable=False,
    )

    assert _durations(result) == [4, 3]


def test_mechanical_stock_splits_collapse_to_one_source_card():
    original = _shot(1, "00:00:00", "00:00:14", "stock_video")
    pieces = split_stock_shots_only([original], 5, variable=False)

    result = collapse_redundant_shot_runs(pieces)

    assert len(result) == 1
    assert result[0].start == "00:00:00"
    assert result[0].end == "00:00:14"
    assert result[0].asset_query == "english stock query"


def test_minimum_duration_removes_one_second_ai_slot_and_keeps_coverage():
    scene = Scene(
        index=1,
        start="00:00:00",
        end="00:00:10",
        title="Scene",
        voice_text="Narration",
        visual_direction="",
        screen_text="",
        music_note="",
        retention_note="",
    )
    shots = [
        _shot(1, "00:00:00", "00:00:04", "stock_video"),
        _shot(2, "00:00:04", "00:00:05", "ai_image_then_i2v"),
        _shot(3, "00:00:05", "00:00:10", "still_kenburns"),
    ]
    shots[1].screen_text = "2008"

    result = enforce_minimum_shot_durations(shots, [scene])

    assert len(result) == 2
    assert [(shot.start, shot.end) for shot in result] == [
        ("00:00:00", "00:00:05"),
        ("00:00:05", "00:00:10"),
    ]
    assert result[0].screen_text == "2008"
    assert [shot.index for shot in result] == [1, 2]
