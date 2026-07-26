from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import voice
from core.script_workflow import default_script


def test_voice_render_stream_emits_live_logs_and_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        voice,
        "_resolve_project",
        lambda _project_id: SimpleNamespace(dir=tmp_path / "project"),
    )

    def fake_render(project_id, request, live_log=None):
        assert project_id == "demo"
        assert request.profile_id == "voice-1"
        assert live_log is not None
        live_log("OmniVoice: fragment 1/2")
        live_log("OmniVoice: fragment 2/2")
        return {
            "ok": True,
            "script": {"voice_profile_id": "voice-1"},
            "path": "voice/narration.wav",
            "duration_sec": 12.5,
        }

    monkeypatch.setattr(voice, "_render_voice_sync", fake_render)
    app = FastAPI()
    app.include_router(voice.router, prefix="/api/projects")

    with TestClient(app) as client:
        response = client.post(
            "/api/projects/demo/tts/render-stream",
            json={"profile_id": "voice-1"},
        )

    assert response.status_code == 200
    assert '"event": "log"' in response.text
    assert "fragment 1/2" in response.text
    assert '"event": "done"' in response.text
    assert '"duration_sec": 12.5' in response.text


def test_voice_render_allows_rewrite_with_important_notices(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    voice_dir = project_dir / "voice"
    voice_dir.mkdir(parents=True)
    text = " ".join(["Сценарий"] * 80)
    script = default_script()
    script.update({
        "mode": "rewrite",
        "source_text": text,
        "final_text": text,
        "target_duration_sec": 600,
        "text_revision": 7,
        "voice_source_revision": 6,
        "voice_outdated": True,
        "agent": {
            **script["agent"],
            "status": "needs_review",
            "stage": "Есть важные замечания",
            "issues": [{
                "id": "notice",
                "type": "unsupported_numbers",
                "severity": "critical",
                "block_index": None,
                "message": "Проверьте число",
                "evidence": "13",
                "suggested_action": "review",
            }],
        },
        "quality": {
            "status": "incomplete",
            "target_words": 100,
            "actual_words": 80,
            "word_ratio": 0.8,
            "total_beats": 1,
            "completed_beats": 1,
            "coverage_ratio": 1.0,
            "accepted_actual": False,
        },
    })
    (project_dir / "00_script.json").write_text(
        json.dumps(script, ensure_ascii=False),
        encoding="utf-8",
    )
    project = SimpleNamespace(
        dir=project_dir,
        voice_dir=voice_dir,
        invalidate_stage=lambda *_args, **_kwargs: None,
    )
    logs: list[str] = []
    monkeypatch.setattr(voice, "_resolve_project", lambda _project_id: project)
    monkeypatch.setattr(
        voice,
        "_profiles",
        lambda _project_dir: [{
            "id": "voice-1",
            "display_name": "Голос",
            "calibrated_wpm": 135,
        }],
    )
    monkeypatch.setattr(
        voice,
        "load_settings",
        lambda: SimpleNamespace(omnivoice_cli="", omnivoice_model=""),
    )
    monkeypatch.setattr(
        voice,
        "render_plan",
        lambda *_args, **_kwargs: {
            "path": "voice/narration.wav",
            "duration_sec": 12.5,
        },
    )

    result = voice._render_voice_sync(
        "demo",
        voice.RenderVoiceRequest(profile_id="voice-1"),
        live_log=logs.append,
    )

    assert result["ok"] is True
    assert result["script"]["voice_source_revision"] == 7
    assert result["script"]["voice_outdated"] is False
    assert any("Озвучка разрешена" in line for line in logs)
