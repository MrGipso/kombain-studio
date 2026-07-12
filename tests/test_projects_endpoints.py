"""Focused API regressions for project identity and job cancellation."""

from fastapi.testclient import TestClient

from backend.main import app
from backend.routers import projects as projects_router
from backend.services import runlog
from core.config import Settings
from core.project import create_project


def _client_with_project_root(monkeypatch, tmp_path):
    settings = Settings(output_base=str(tmp_path))
    monkeypatch.setattr(projects_router, "_settings", lambda: settings)
    return TestClient(app), settings


def test_cancel_existing_project_uses_resolved_project_id(monkeypatch, tmp_path):
    client, settings = _client_with_project_root(monkeypatch, tmp_path)
    project = create_project("Cancelable", tmp_path, settings=settings)
    requested: list[str] = []

    def request_cancel(project_id: str) -> str:
        requested.append(project_id)
        return "Export"

    monkeypatch.setattr(projects_router.jobs, "request_cancel", request_cancel)
    with client:
        response = client.post(f"/api/projects/{project.dir.name}/cancel")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "cancelled": "Export"}
    assert requested == [project.dir.name]


def test_cancel_missing_project_returns_404(monkeypatch, tmp_path):
    client, _settings = _client_with_project_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        projects_router.jobs,
        "request_cancel",
        lambda _project_id: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    with client:
        response = client.post("/api/projects/missing/cancel")

    assert response.status_code == 404


def test_create_project_slug_collision_returns_409(monkeypatch, tmp_path):
    client, _settings = _client_with_project_root(monkeypatch, tmp_path)
    with client:
        first = client.post("/api/projects", json={"name": "My Video"})
        collision = client.post("/api/projects", json={"name": "My   Video"})

    assert first.status_code == 200
    assert collision.status_code == 409
    assert "уже существует" in collision.json()["detail"]
    assert not (tmp_path / "my-video-2").exists()


def test_projects_list_ignores_cache_and_keeps_legacy_manifest(monkeypatch, tmp_path):
    client, _settings = _client_with_project_root(monkeypatch, tmp_path)
    cache = tmp_path / ".uv-cache"
    cache.mkdir()
    arbitrary = tmp_path / "random"
    arbitrary.mkdir()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / ".project.json").write_text(
        '{"name": "Legacy project", "preset": "universal"}',
        encoding="utf-8",
    )

    with client:
        response = client.get("/api/projects")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["Legacy project"]


def test_project_run_logs_are_typed_scoped_and_limit_is_validated(monkeypatch, tmp_path):
    client, settings = _client_with_project_root(monkeypatch, tmp_path)
    project = create_project("Logged", tmp_path, settings=settings)
    runlog.append(
        project.dir,
        "Export",
        "Master ready",
        run_id="run-123",
        job_id="job-123",
        event="result",
        fields={"profile": "master"},
    )

    with client:
        response = client.get(f"/api/projects/{project.dir.name}/run-logs?limit=1")
        too_large = client.get(f"/api/projects/{project.dir.name}/run-logs?limit=501")
        missing = client.get("/api/projects/missing/run-logs")

    assert response.status_code == 200
    assert response.json() == {
        "items": [{
            "v": 1,
            "ts": response.json()["items"][0]["ts"],
            "job": "Export",
            "msg": "Master ready",
            "run_id": "run-123",
            "job_id": "job-123",
            "event": "result",
            "level": "info",
            "fields": {"profile": "master"},
        }],
    }
    assert too_large.status_code == 422
    assert missing.status_code == 404
