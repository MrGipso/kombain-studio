from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import transcription


class FakeProject:
    def __init__(self, directory: Path):
        self.dir = directory
        self.voice_dir = directory / "voice"
        self.voice_dir.mkdir(parents=True)
        self.invalidations: list[tuple[str, str]] = []

    def invalidate_stage(self, stage: str, reason: str = ""):
        self.invalidations.append((stage, reason))


def test_explicit_selection_wins_over_narration_default(tmp_path):
    project = FakeProject(tmp_path / "project")
    narration = project.voice_dir / "narration.wav"
    uploaded = project.voice_dir / "interview.wav"
    narration.write_bytes(b"narration")
    uploaded.write_bytes(b"uploaded")

    transcription._select_audio(project, uploaded, origin="upload")

    assert transcription._preferred_audio(project) == uploaded
    payload = transcription._source_payload(project)
    assert payload["filename"] == "interview.wav"
    assert payload["selected"] is True
    assert payload["origin"] == "upload"
    narration_payload = transcription._source_payload(project, "narration.wav")
    assert narration_payload["filename"] == "narration.wav"
    assert narration_payload["selected"] is False
    assert narration_payload["origin"] == "omnivoice"


def test_narration_handoff_selects_source_and_returns_player_url(tmp_path, monkeypatch):
    project = FakeProject(tmp_path / "project")
    (project.voice_dir / "narration.wav").write_bytes(b"narration")
    monkeypatch.setattr(transcription, "_resolve_project", lambda _project_id: project)

    @contextmanager
    def mutation(*_args, **_kwargs):
        yield

    monkeypatch.setattr(transcription.jobs, "project_mutation", mutation)
    app = FastAPI()
    app.include_router(transcription.router, prefix="/api/projects")

    with TestClient(app) as client:
        response = client.post("/api/projects/demo/transcription-source/narration")
        source_response = client.get("/api/projects/demo/transcription-source")

    assert response.status_code == 200
    assert source_response.status_code == 200
    payload = source_response.json()
    assert payload["available"] is True
    assert payload["filename"] == "narration.wav"
    assert payload["selected"] is True
    assert payload["origin"] == "omnivoice"
    assert payload["url"].startswith("/api/media/")
    assert project.invalidations
