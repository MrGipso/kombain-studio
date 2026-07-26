from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.routers import scenes as scenes_router
from backend.services import jobs
from core.config import Settings
from core.project import create_project


@pytest.fixture(autouse=True)
def isolated_jobs_database(tmp_path, monkeypatch):
    monkeypatch.setenv(jobs.JOBS_DB_ENV, str(tmp_path / "jobs.sqlite3"))


def _scene(index: int, start: str, end: str) -> dict:
    return {
        "index": index,
        "start": start,
        "end": end,
        "title": f"Scene {index}",
        "voice_text": f"Narration {index}",
        "visual_direction": "Wide shot",
        "screen_text": "",
        "music_note": "",
        "retention_note": "",
    }


def _project(tmp_path: Path, monkeypatch):
    settings = Settings(output_base=str(tmp_path))
    project = create_project("Scene editor", tmp_path, settings=settings)
    project.write_json(
        "scenes",
        [
            _scene(1, "00:00:00.000", "00:00:05.000"),
            _scene(2, "00:00:05.000", "00:00:10.000"),
            _scene(3, "00:00:10.000", "00:00:15.000"),
        ],
    )
    monkeypatch.setattr(scenes_router, "load_settings", lambda: settings)
    return project


def _snapshot(project) -> dict[str, bytes]:
    return {
        path.relative_to(project.dir).as_posix(): path.read_bytes()
        for path in project.dir.rglob("*")
        if path.is_file()
    }


def _assert_rejected_without_changes(project, payload: dict, message: str) -> None:
    before = _snapshot(project)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(scenes_router.update_scene(project.dir.name, 2, payload))
    assert caught.value.status_code == 422
    assert message in str(caught.value.detail)
    assert _snapshot(project) == before


def test_scene_edit_rejects_index_reassignment_without_writing(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    _assert_rejected_without_changes(project, {"index": 7}, "immutable")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"start": "not-a-timecode"}, "HH:MM:SS"),
        ({"start": "00:60:00"}, "HH:MM:SS"),
        ({"start": "00:00:09", "end": "00:00:09"}, "earlier"),
        ({"start": "00:00:04.999"}, "previous"),
        ({"end": "00:00:10.001"}, "next"),
        ({"title": "   "}, "cannot be empty"),
    ],
)
def test_scene_edit_rejects_invalid_timeline_without_writing(
    tmp_path, monkeypatch, payload, message,
):
    project = _project(tmp_path, monkeypatch)
    _assert_rejected_without_changes(project, payload, message)


def test_scene_edit_accepts_valid_fractional_boundaries_and_returns_saved_scene(
    tmp_path, monkeypatch,
):
    project = _project(tmp_path, monkeypatch)

    updated = asyncio.run(scenes_router.update_scene(
        project.dir.name,
        2,
        {
            "start": "00:00:05.250",
            "end": "00:00:09.750",
            "title": "Corrected scene",
        },
    ))

    assert updated["index"] == 2
    assert updated["start"] == "00:00:05.250"
    assert updated["end"] == "00:00:09.750"
    assert updated["title"] == "Corrected scene"
    assert project.read_json("scenes")[1] == updated


def test_scene_edit_normalizes_srt_fraction_and_surrounding_whitespace(
    tmp_path, monkeypatch,
):
    project = _project(tmp_path, monkeypatch)

    updated = asyncio.run(scenes_router.update_scene(
        project.dir.name,
        2,
        {
            "start": " 00:00:05,250 ",
            "end": " 00:00:09,750 ",
            "title": "  Corrected scene  ",
        },
    ))

    assert updated["start"] == "00:00:05.250"
    assert updated["end"] == "00:00:09.750"
    assert updated["title"] == "Corrected scene"


def test_scene_edit_does_not_mark_stale_analysis_current(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    original = project.read_json("scenes")
    project.mark_stage_completed("scenes", original, input_hash="old-settings")
    project.invalidate_stage("scenes", reason="global LM settings changed")

    asyncio.run(scenes_router.update_scene(
        project.dir.name,
        2,
        {"title": "Manually corrected, but not reanalyzed"},
    ))

    state = project.stage_states()["scenes"]
    assert state["status"] == "stale"
    assert state["error"] == "global LM settings changed"


def test_scene_edit_does_not_clear_a_failed_reanalysis(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    original = project.read_json("scenes")
    project.mark_stage_completed("scenes", original, input_hash="old-settings")
    project.mark_stage_failed(
        "scenes", "embedding model failed", input_hash="new-settings",
    )

    asyncio.run(scenes_router.update_scene(
        project.dir.name,
        2,
        {"title": "Manual correction on retained old plan"},
    ))

    state = project.stage_states()["scenes"]
    assert state["status"] == "failed"
    assert state["error"] == "embedding model failed"
    assert state["input_hash"] == "new-settings"


def test_title_only_edit_migrates_legacy_minute_timecodes(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    legacy = project.read_json("scenes")
    for index, scene in enumerate(legacy):
        scene["start"] = f"00:{index * 5:02d}"
        scene["end"] = f"00:{(index + 1) * 5:02d}"
    project.write_json("scenes", legacy)

    updated = asyncio.run(scenes_router.update_scene(
        project.dir.name,
        2,
        {"title": "Legacy scene corrected"},
    ))

    assert updated["start"] == "00:00:05"
    assert updated["end"] == "00:00:10"
    assert updated["title"] == "Legacy scene corrected"


def test_full_ui_payload_keeps_unchanged_legacy_timecodes_editable(
    tmp_path, monkeypatch,
):
    project = _project(tmp_path, monkeypatch)
    legacy = project.read_json("scenes")
    for index, scene in enumerate(legacy):
        scene["start"] = f"00:{index * 5:02d}"
        scene["end"] = f"00:{(index + 1) * 5:02d}"
    project.write_json("scenes", legacy)
    payload = {**legacy[1], "title": "Saved by the scene editor"}

    updated = asyncio.run(scenes_router.update_scene(
        project.dir.name,
        2,
        payload,
    ))

    assert updated["start"] == "00:00:05"
    assert updated["end"] == "00:00:10"
    assert updated["title"] == "Saved by the scene editor"


def test_new_timing_edit_still_requires_full_hour_timecode(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    _assert_rejected_without_changes(project, {"start": "00:06"}, "HH:MM:SS")


def test_scene_edit_rejects_duplicate_indexes_in_existing_plan(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    invalid = project.read_json("scenes")
    invalid[2]["index"] = 2
    project.write_json("scenes", invalid)
    before = _snapshot(project)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(scenes_router.update_scene(project.dir.name, 2, {"title": "Changed"}))

    assert caught.value.status_code == 409
    assert "duplicate" in str(caught.value.detail)
    assert _snapshot(project) == before


def test_scene_edit_rejects_out_of_order_indexes_in_existing_plan(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    invalid = project.read_json("scenes")
    invalid[1]["index"], invalid[2]["index"] = 3, 2
    project.write_json("scenes", invalid)
    before = _snapshot(project)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(scenes_router.update_scene(project.dir.name, 3, {"title": "Changed"}))

    assert caught.value.status_code == 409
    assert "out of order" in str(caught.value.detail)
    assert _snapshot(project) == before


def test_scene_export_neutralizes_formulas_and_markdown_in_generated_fields(
    tmp_path, monkeypatch,
):
    project = _project(tmp_path, monkeypatch)
    generated = project.read_json("scenes")
    generated[0].update(
        title="=HYPERLINK(\"https://example.test\")",
        visual_direction="@SUM(1+1)",
        screen_text="# injected\nheading",
        music_note="[label](https://example.test)",
    )
    project.write_json("scenes", generated)

    result = asyncio.run(scenes_router.export_scenes(project.dir.name))

    assert result["count"] == 3
    csv_text = (project.dir / "edit_plan.csv").read_text("utf-8")
    markdown = (project.dir / "visual_plan.md").read_text("utf-8")
    assert "'=HYPERLINK" in csv_text
    assert "'@SUM" in csv_text
    assert "\\# injected heading" in markdown
    assert "\\[label\\](https://example.test)" in markdown
