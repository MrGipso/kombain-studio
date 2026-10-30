from __future__ import annotations

import asyncio
import io
from contextlib import contextmanager

import pytest
from fastapi import HTTPException, UploadFile

from backend.routers import media, music, sfx
from backend.services import jobs
from core import board as board_store
from core.config import Settings
from core.models import AssetCard
from core.project import create_project


def _card() -> AssetCard:
    return AssetCard(
        shot_index=1,
        scene_index=1,
        scene_title="Scene",
        start="00:00:00",
        end="00:00:05",
        visual_type="ai_image",
        route="image",
        description="A quiet room",
        asset_query="quiet room",
        image_model="krea2",
        video_model="",
        remotion_template="",
        image_prompt="A quiet room",
        video_prompt="",
        screen_text="",
        motion="static",
        fallback="still",
        image_output="",
        video_output="",
        remotion_output="",
        status="planned",
        notes="",
        approved=False,
        sfx_query="room tone",
    )


def _project(tmp_path):
    settings = Settings(output_base=str(tmp_path))
    project = create_project("Locked routes", tmp_path, settings=settings)
    board_store.write_asset_board(project.dir, [_card()])
    return project, settings


def _bytes_snapshot(project_dir):
    return {
        path.relative_to(project_dir).as_posix(): path.read_bytes()
        for path in project_dir.rglob("*")
        if path.is_file()
    }


def _assert_busy(coro):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(coro)
    assert caught.value.status_code == 409
    assert "Проект занят" in str(caught.value.detail)


def test_sfx_query_busy_returns_409_without_changing_project_bytes(tmp_path, monkeypatch):
    project, settings = _project(tmp_path)
    monkeypatch.setattr(sfx, "load_settings", lambda: settings)
    before = _bytes_snapshot(project.dir)

    with jobs.project_mutation(project.dir.name, "Активный рендер"):
        _assert_busy(
            sfx.set_query(
                project.dir.name,
                sfx.QueryRequest(card_indices=[0], sfx_query="new ambience"),
            )
        )

    assert _bytes_snapshot(project.dir) == before


def test_sfx_upload_busy_cleans_staging_and_changes_no_project_bytes(tmp_path, monkeypatch):
    project, settings = _project(tmp_path)
    monkeypatch.setattr(sfx, "load_settings", lambda: settings)
    before = _bytes_snapshot(project.dir)
    upload = UploadFile(io.BytesIO(b"RIFF-test-audio"), filename="custom.wav")

    with jobs.project_mutation(project.dir.name, "Активный рендер"):
        _assert_busy(sfx.upload(project.dir.name, upload, "0"))

    assert _bytes_snapshot(project.dir) == before
    assert not list(project.dir.glob(".sfx-upload-*.tmp"))


def test_timeline_settings_busy_returns_409_without_changing_project_bytes(
    tmp_path, monkeypatch
):
    project, settings = _project(tmp_path)
    monkeypatch.setattr(media, "load_settings", lambda: settings)
    before = _bytes_snapshot(project.dir)

    with jobs.project_mutation(project.dir.name, "Активный экспорт"):
        _assert_busy(
            media.save_timeline_settings(
                project.dir.name,
                media.TimelineSettings(voice_start="00:00:12"),
            )
        )

    assert _bytes_snapshot(project.dir) == before


@pytest.mark.parametrize("operation", ["settings", "select", "trim", "delete"])
def test_short_music_mutations_busy_return_409_and_preserve_all_bytes(
    operation, tmp_path, monkeypatch
):
    project, settings = _project(tmp_path)
    monkeypatch.setattr(music, "load_settings", lambda: settings)
    music_dir = project.dir / "music"
    music_dir.mkdir()
    track = music_dir / "track.wav"
    track.write_bytes(b"RIFF-original-track")
    (music_dir / "tracks.json").write_text(
        '{"track.wav":{"trim_start":1,"trim_end":4}}', encoding="utf-8"
    )
    project.metadata["music_selected"] = track.name
    project.save_meta()
    before = _bytes_snapshot(project.dir)

    if operation == "settings":
        request = music.update_music_settings(
            project.dir.name,
            music.MusicSettingsRequest(music_tags="changed", music_bpm=120),
        )
    elif operation == "select":
        request = music.select_track(
            project.dir.name, music.SelectTrackRequest(filename=track.name)
        )
    elif operation == "trim":
        request = music.set_trim(
            project.dir.name,
            music.TrimRequest(filename=track.name, trim_start=2, trim_end=3),
        )
    else:
        request = music.delete_track(project.dir.name, track.name)

    with jobs.project_mutation(project.dir.name, "Активная генерация"):
        _assert_busy(request)

    assert _bytes_snapshot(project.dir) == before


def test_music_brief_commits_metadata_and_invalidation_inside_project_job(
    tmp_path, monkeypatch
):
    project, settings = _project(tmp_path)
    project.write_json("scenes", [{"music_note": "calm and sparse"}])
    monkeypatch.setattr(music, "load_settings", lambda: settings)
    monkeypatch.setattr(music, "_resolve_project", lambda _project_id: project)
    monkeypatch.setattr(
        music,
        "_lm_music_brief",
        lambda *_args, **_kwargs: {"tags": "ambient piano", "bpm": 82},
    )
    monkeypatch.setattr(music, "_unload_lm_after_brief", lambda _settings: None)

    active = False
    committed: list[str] = []
    original_save_meta = project.save_meta
    original_invalidate = project.invalidate_stage

    @contextmanager
    def guarded_job(*_args, **_kwargs):
        nonlocal active
        active = True
        try:
            yield None
        finally:
            active = False

    def save_meta():
        assert active
        committed.append("metadata")
        original_save_meta()

    def invalidate(*args, **kwargs):
        assert active
        committed.append("manifest")
        return original_invalidate(*args, **kwargs)

    monkeypatch.setattr(music.jobs, "project_job", guarded_job)
    monkeypatch.setattr(project, "save_meta", save_meta)
    monkeypatch.setattr(project, "invalidate_stage", invalidate)

    payload = asyncio.run(music.generate_music_brief(project.dir.name))

    assert committed == ["metadata", "manifest"]
    assert payload["music_tags"] == "ambient piano"
    assert payload["music_bpm"] == 82
