"""Cancellation must reach ComfyUI polling, not wait for a long render."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import comfy
from core import comfy_generation as generation
from backend.routers import assets as assets_router
from backend.routers import music as music_router
from backend.services import jobs


class _CancelledInPoll(Exception):
    pass


def _raise_cancelled() -> bool:
    raise _CancelledInPoll("stop")


def test_history_poll_propagates_job_cancelled_without_an_http_request(monkeypatch):
    monkeypatch.setattr(
        comfy,
        "comfy_request_json",
        lambda *_args, **_kwargs: pytest.fail(
            "cancelled history polling must not make another HTTP request"
        ),
    )

    def check_job() -> bool:
        raise jobs.JobCancelled("Задача отменена пользователем")

    with pytest.raises(jobs.JobCancelled, match="отменена пользователем"):
        comfy.comfy_wait_for_history(
            "http://comfy", "prompt-id", cancel_check=check_job
        )


def test_image_generator_forwards_cancel_check_to_history(monkeypatch, tmp_path):
    card = SimpleNamespace(
        image_model="krea2_turbo",
        image_prompt="a clean documentary frame",
        shot_index=1,
    )
    monkeypatch.setattr(generation, "prepare_image_workflow", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(generation, "comfy_queue_prompt", lambda *_args: "image-prompt")

    def wait(_base_url, _prompt_id, **kwargs):
        kwargs["cancel_check"]()
        raise AssertionError("unreachable")

    monkeypatch.setattr(generation, "comfy_wait_for_history", wait)

    with pytest.raises(_CancelledInPoll, match="stop"):
        generation.generate_comfy_image(
            "http://comfy", tmp_path, card, cancel_check=_raise_cancelled
        )


def test_ltx_generator_forwards_cancel_check_to_history(monkeypatch, tmp_path):
    image = tmp_path / "input.png"
    image.write_bytes(b"image")
    card = SimpleNamespace(
        image_output=str(image),
        video_prompt="slow camera move",
        image_prompt="",
        shot_index=2,
        start="00:00:00",
        end="00:00:04",
        priority="medium",
        ltx_draft=False,
    )
    monkeypatch.setattr(generation, "comfy_upload_image", lambda *_args: "input.png")
    monkeypatch.setattr(generation, "project_video_style_id", lambda *_args: "default")
    monkeypatch.setattr(
        generation,
        "get_video_style",
        lambda *_args: SimpleNamespace(negative_prompt=""),
    )
    monkeypatch.setattr(generation, "prepare_ltx_workflow", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(generation, "comfy_queue_prompt", lambda *_args: "video-prompt")

    def wait(_base_url, _prompt_id, **kwargs):
        assert kwargs["timeout_seconds"] == 3600
        kwargs["cancel_check"]()
        raise AssertionError("unreachable")

    monkeypatch.setattr(generation, "comfy_wait_for_history", wait)

    with pytest.raises(_CancelledInPoll, match="stop"):
        generation.generate_comfy_ltx_video(
            "http://comfy",
            tmp_path,
            card,
            draft=False,
            cancel_check=_raise_cancelled,
        )


def test_music_generator_forwards_cancel_check_to_history(monkeypatch, tmp_path):
    monkeypatch.setattr(generation, "prepare_music_workflow", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(generation, "comfy_queue_prompt", lambda *_args: "music-prompt")

    def wait(_base_url, _prompt_id, **kwargs):
        assert kwargs["timeout_seconds"] == 1800
        kwargs["cancel_check"]()
        raise AssertionError("unreachable")

    monkeypatch.setattr(generation, "comfy_wait_for_history", wait)

    with pytest.raises(_CancelledInPoll, match="stop"):
        generation.generate_comfy_music(
            "http://comfy",
            tmp_path,
            "ambient",
            cancel_check=_raise_cancelled,
        )


def test_asset_generation_reports_poll_cancellation_as_conflict(monkeypatch, tmp_path):
    app = FastAPI()
    app.include_router(assets_router.router, prefix="/api/projects")
    monkeypatch.setattr(assets_router, "_resolve_project", lambda _project_id: tmp_path)

    @contextmanager
    def project_job(*_args, **_kwargs):
        yield object()

    checks = 0

    def check(_event):
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise jobs.JobCancelled("Задача отменена пользователем")

    def generate(*_args, **kwargs):
        assert callable(kwargs["cancel_check"])
        kwargs["cancel_check"]()
        raise AssertionError("unreachable")

    monkeypatch.setattr(assets_router.jobs, "project_job", project_job)
    monkeypatch.setattr(assets_router.jobs, "check", check)
    monkeypatch.setattr(
        assets_router,
        "_require_card_operation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(assets_router, "_generate_sync", generate)

    with TestClient(app) as client:
        response = client.post(
            "/api/projects/example/generate/0",
            headers={"If-Match": '"test"', "X-Card-ID": "card-test"},
        )

    assert response.status_code == 409
    assert "отменена пользователем" in response.text
    assert checks == 2


def test_music_sse_marks_poll_cancellation_explicitly(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = SimpleNamespace(
        dir=project_dir,
        metadata={"music_tags": "ambient", "music_bpm": 90, "music_duration": 30},
        settings=SimpleNamespace(comfy_url="http://comfy"),
    )
    app = FastAPI()
    app.include_router(music_router.router, prefix="/api/projects")
    monkeypatch.setattr(music_router, "_resolve_project", lambda _project_id: project)

    @contextmanager
    def project_job(*_args, **_kwargs):
        yield object()

    checks = 0

    def check(_event):
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise jobs.JobCancelled("Задача отменена пользователем")

    def generate_music(*_args, **kwargs):
        assert callable(kwargs["cancel_check"])
        kwargs["cancel_check"]()
        raise AssertionError("unreachable")

    monkeypatch.setattr(music_router.jobs, "project_job", project_job)
    monkeypatch.setattr(music_router.jobs, "check", check)
    monkeypatch.setattr(
        music_router.comfy_generation,
        "generate_comfy_music",
        generate_music,
    )

    with TestClient(app) as client:
        response = client.post("/api/projects/example/music/generate")

    assert response.status_code == 200
    assert '"event": "error"' in response.text
    assert '"code": "cancelled"' in response.text
    assert "отменена пользователем" in response.text
    assert checks == 2
