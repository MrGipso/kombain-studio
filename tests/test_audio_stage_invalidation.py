import asyncio

from core.config import Settings
from core.project import Project
from backend.routers import music, settings as settings_router


def test_music_settings_invalidate_current_timeline(tmp_path, monkeypatch):
    project = Project(name="demo", dir=tmp_path)
    project.mark_stage_completed("timeline", {"duration": 10})
    project.mark_stage_completed("validation", {"summary": {}})
    monkeypatch.setattr(music, "_resolve_project", lambda _project_id: project)

    asyncio.run(music.update_music_settings(
        "demo",
        music.MusicSettingsRequest(music_volume_db=-12),
    ))

    assert project.stage_state("timeline")["status"] == "stale"
    assert project.stage_state("validation")["status"] == "stale"


def test_global_audio_and_lm_changes_invalidate_each_affected_stage(tmp_path, monkeypatch):
    initial = Settings(output_base=str(tmp_path), lm_model="old", sfx_enabled=False)

    class RecordingProject:
        def __init__(self):
            self.dir = tmp_path / "recording-project"
            self.dir.mkdir()
            self.calls: list[tuple[str, str | None]] = []

        def invalidate_stage(self, stage: str, reason: str | None = None):
            self.calls.append((stage, reason))

    project = RecordingProject()
    monkeypatch.setattr(settings_router, "load_settings", lambda: initial)
    monkeypatch.setattr(settings_router, "save_settings", lambda _settings: None)
    monkeypatch.setattr(settings_router, "list_projects", lambda _base: [project])
    monkeypatch.setattr(settings_router, "open_project", lambda *_args, **_kwargs: project)

    asyncio.run(settings_router.update_settings({"lm_model": "new", "sfx_enabled": True}))

    assert [stage for stage, _reason in project.calls] == ["scenes", "timeline"]
