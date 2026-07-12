import asyncio

from backend.routers import projects as projects_router
from core.config import Settings
from core.project import create_project


def test_project_state_exposes_manifest_and_legacy_booleans(tmp_path, monkeypatch):
    settings = Settings(output_base=str(tmp_path))
    project = create_project("Demo", tmp_path, settings=settings)
    project.mark_stage_completed("transcript", {"segments": [1]})
    project.mark_stage_completed("scenes", [{"index": 1}])
    project.mark_stage_completed("shots", [{"index": 1}])

    # Changing scenes invalidates the downstream shot artifact.
    project.mark_stage_completed("scenes", [{"index": 1}, {"index": 2}])
    monkeypatch.setattr(projects_router, "_settings", lambda: settings)

    state = asyncio.run(projects_router.get_project_state(project.dir.name))

    assert state["schema_version"] == 1
    assert state["steps"]["transcript"] is True
    assert state["steps"]["scenes"] is True
    assert state["steps"]["shot_list"] is False
    assert state["stage_states"]["shots"]["status"] == "stale"
    assert state["stage_states"]["shots"]["invalidated_by"] == "scenes"
