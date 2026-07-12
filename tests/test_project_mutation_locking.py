"""Regression tests for short project writes racing long-running jobs.

Every endpoint below must use the same per-project lock as ``project_job``.
The important contract is stronger than merely returning 409: a rejected write
must leave every project artifact (including the stage manifest) byte-identical.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from backend.routers import projects, scenes, script, transcription
from backend.services import jobs
from core.config import Settings
from core.project import create_project


@pytest.fixture(autouse=True)
def isolated_jobs_database(tmp_path, monkeypatch):
    monkeypatch.setenv(jobs.JOBS_DB_ENV, str(tmp_path / "jobs.sqlite3"))


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _assert_busy_without_changes(project, awaitable_factory) -> None:
    before = _snapshot(project.dir)
    with jobs.project_job(project.dir.name, "Фоновый рендер"):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(awaitable_factory())
    assert caught.value.status_code == 409
    assert _snapshot(project.dir) == before


def _scene(index: int = 1) -> dict:
    return {
        "index": index,
        "start": "00:00:00.000",
        "end": "00:00:05.000",
        "title": "Original scene",
        "voice_text": "Narration",
        "visual_direction": "Wide shot",
        "screen_text": "",
        "music_note": "",
        "retention_note": "",
    }


def test_project_settings_reject_busy_project_without_metadata_or_manifest_change(
    tmp_path, monkeypatch,
):
    config = Settings(output_base=str(tmp_path))
    project = create_project("Locked settings", tmp_path, settings=config)
    project.mark_stage_completed("scenes", [_scene()])
    monkeypatch.setattr(projects, "_settings", lambda: config)

    request = projects.UpdateProjectSettingsRequest(video_style="cinematic")
    _assert_busy_without_changes(
        project,
        lambda: projects.update_project_settings(project.dir.name, request),
    )


def test_scene_edit_rejects_busy_project_without_artifact_or_manifest_change(
    tmp_path, monkeypatch,
):
    config = Settings(output_base=str(tmp_path))
    project = create_project("Locked scene", tmp_path, settings=config)
    project.write_json("scenes", [_scene()])
    project.mark_stage_completed("scenes", [_scene()])
    monkeypatch.setattr(scenes, "load_settings", lambda: config)

    _assert_busy_without_changes(
        project,
        lambda: scenes.update_scene(project.dir.name, 1, {"title": "Must not be saved"}),
    )


def test_scene_plan_export_rejects_busy_project_without_overwriting_outputs(
    tmp_path, monkeypatch,
):
    config = Settings(output_base=str(tmp_path))
    project = create_project("Locked scene export", tmp_path, settings=config)
    project.write_json("scenes", [_scene()])
    (project.dir / "edit_plan.csv").write_bytes(b"existing csv")
    (project.dir / "visual_plan.md").write_bytes(b"existing markdown")
    monkeypatch.setattr(scenes, "load_settings", lambda: config)

    _assert_busy_without_changes(
        project,
        lambda: scenes.export_scenes(project.dir.name),
    )


def test_script_save_rejects_busy_project_without_artifact_or_manifest_change(
    tmp_path, monkeypatch,
):
    config = Settings(output_base=str(tmp_path))
    project = create_project("Locked script", tmp_path, settings=config)
    script._write_script(project.dir, {**script._default_script(), "topic": "Original"})
    project.mark_stage_completed("script", {"topic": "Original"})
    monkeypatch.setattr(script, "load_settings", lambda: config)

    _assert_busy_without_changes(
        project,
        lambda: script.save_script(project.dir.name, {"topic": "Must not be saved"}),
    )


def test_script_export_rejects_busy_project_without_overwriting_text(
    tmp_path, monkeypatch,
):
    config = Settings(output_base=str(tmp_path))
    project = create_project("Locked script export", tmp_path, settings=config)
    script._write_script(project.dir, {
        **script._default_script(),
        "hooks": ["Hook"],
        "beats": [{"text": "Generated narration"}],
    })
    (project.dir / script.SCRIPT_TEXT_FILE).write_bytes(b"existing narration")
    monkeypatch.setattr(script, "load_settings", lambda: config)

    _assert_busy_without_changes(
        project,
        lambda: script.export_script(project.dir.name),
    )


def test_audio_upload_rejects_busy_project_and_cleans_temporary_file(
    tmp_path, monkeypatch,
):
    config = Settings(output_base=str(tmp_path))
    project = create_project("Locked upload", tmp_path, settings=config)
    destination = project.voice_dir / "voice.wav"
    destination.write_bytes(b"existing audio")
    project.mark_stage_completed("transcript", {"text": "existing"})
    monkeypatch.setattr(transcription, "load_settings", lambda: config)

    _assert_busy_without_changes(
        project,
        lambda: transcription.upload_audio(
            project.dir.name,
            UploadFile(filename="voice.wav", file=io.BytesIO(b"replacement audio")),
        ),
    )
    assert destination.read_bytes() == b"existing audio"
    assert not list(project.voice_dir.glob(".upload-*.part"))
