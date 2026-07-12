import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.routers import settings as settings_router
from backend.services import jobs
from core.config import Settings, load_settings, save_settings
from core.project import create_project, open_project


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _route_settings(monkeypatch, settings_path: Path) -> None:
    monkeypatch.setattr(
        settings_router,
        "load_settings",
        lambda: load_settings(settings_path),
    )
    monkeypatch.setattr(
        settings_router,
        "save_settings",
        lambda value: save_settings(value, settings_path),
    )


def test_put_settings_busy_is_409_and_changes_no_files(tmp_path, monkeypatch):
    project_root = tmp_path / "projects"
    current = Settings(output_base=str(project_root), lm_model="before")
    alpha = create_project("Alpha", project_root, settings=current)
    zulu = create_project("Zulu", project_root, settings=current)
    alpha.mark_stage_completed("scenes", content={"version": 1})
    zulu.mark_stage_completed("scenes", content={"version": 1})

    settings_path = tmp_path / "settings.json"
    save_settings(current, settings_path)
    _route_settings(monkeypatch, settings_path)
    monkeypatch.setenv("PIPELINE_JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    jobs._initialized_db_path = None

    settings_before = settings_path.read_bytes()
    projects_before = _snapshot(project_root)

    # Alpha sorts first and is acquired temporarily; Zulu being busy must make
    # the entire bulk acquisition fail before the settings file is written.
    with jobs.project_job(zulu.dir.name, "Active render"):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(settings_router.update_settings({"lm_model": "after"}))

        assert caught.value.status_code == 409
        assert "Active render" in str(caught.value.detail)
        assert jobs.running_job(alpha.dir.name) == ""
        with jobs.project_mutation(alpha.dir.name, "Proof partial lock was released"):
            pass

    assert settings_path.read_bytes() == settings_before
    assert _snapshot(project_root) == projects_before


def test_put_settings_saves_and_invalidates_every_project_under_bulk_locks(
    tmp_path,
    monkeypatch,
):
    project_root = tmp_path / "projects"
    current = Settings(output_base=str(project_root), lm_model="before")
    projects = [
        create_project("Alpha", project_root, settings=current),
        create_project("Zulu", project_root, settings=current),
    ]
    for project in projects:
        project.mark_stage_completed("scenes", content={"project": project.name})

    settings_path = tmp_path / "settings.json"
    save_settings(current, settings_path)
    monkeypatch.setattr(settings_router, "load_settings", lambda: load_settings(settings_path))

    expected_ids = {project.dir.name for project in projects}
    invalidated: list[str] = []

    def guarded_save(value: Settings) -> None:
        assert all(
            jobs.running_job(project_id) == "Global settings update"
            for project_id in expected_ids
        )
        save_settings(value, settings_path)

    def guarded_open(path: Path, settings: Settings | None = None):
        assert jobs.running_job(path.name) == "Global settings update"
        project = open_project(path, settings=settings)
        assert project is not None
        original_invalidate = project.invalidate_stage

        def guarded_invalidate(*args, **kwargs):
            assert all(
                jobs.running_job(project_id) == "Global settings update"
                for project_id in expected_ids
            )
            invalidated.append(path.name)
            return original_invalidate(*args, **kwargs)

        project.invalidate_stage = guarded_invalidate
        return project

    monkeypatch.setattr(settings_router, "save_settings", guarded_save)
    monkeypatch.setattr(settings_router, "open_project", guarded_open)

    result = asyncio.run(settings_router.update_settings({"lm_model": "after"}))

    assert result == {"ok": True}
    assert load_settings(settings_path).lm_model == "after"
    assert set(invalidated) == expected_ids
    for project in projects:
        refreshed = open_project(project.dir, settings=current)
        assert refreshed is not None
        assert refreshed.stage_state("scenes")["status"] == "stale"
        assert jobs.running_job(project.dir.name) == ""
        with jobs.project_mutation(project.dir.name, "Post-update edit"):
            pass
