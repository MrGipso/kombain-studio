from __future__ import annotations

from pathlib import Path

from backend.services import pipeline_orch
from core import script_align
from core.models import Scene


def _settings() -> dict:
    return {
        "lm_url": "http://lm.test/v1",
        "lm_model": "chat-model",
        "lm_embed_model": "embedding-model",
        "lm_auto_unload": True,
        "lm_unload_delay": "0",
    }


def _segments() -> list[dict]:
    return [
        {"start": "00:00:00", "end": "00:00:20", "text": "Первая часть"},
        {"start": "00:00:20", "end": "00:00:40", "text": "Вторая часть"},
    ]


def _scene() -> Scene:
    return Scene(
        index=1,
        start="00:00:00",
        end="00:00:40",
        title="Сцена",
        voice_text="Первая часть Вторая часть",
        visual_direction="",
        screen_text="",
        music_note="",
        retention_note="",
    )


def test_embedding_model_is_released_before_chat_model_is_loaded(
    tmp_path: Path, monkeypatch,
):
    events: list[str] = []
    logs: list[str] = []
    monkeypatch.setattr(script_align, "scenes_from_script", lambda *_args: None)
    monkeypatch.setattr(
        pipeline_orch.scene_tools,
        "build_scenes_by_embedding",
        lambda *_args, **_kwargs: events.append("embed") or [_scene()],
    )
    monkeypatch.setattr(
        pipeline_orch,
        "_maybe_unload_lm",
        lambda _settings, _reason, _log, model=None: events.append(f"unload:{model}"),
    )
    monkeypatch.setattr(
        pipeline_orch,
        "_ensure_lm_context",
        lambda *_args: events.append("load:chat"),
    )
    monkeypatch.setattr(
        pipeline_orch.scene_tools,
        "enrich_scenes_with_lm",
        lambda scenes, *_args, **_kwargs: events.append("enrich") or scenes,
    )

    pipeline_orch.run_scene_analysis(tmp_path, _segments(), _settings(), logs.append)

    assert events[:4] == [
        "embed", "unload:embedding-model", "load:chat", "enrich",
    ], logs


def test_failed_embedding_attempt_is_released_before_lm_grouping(
    tmp_path: Path, monkeypatch,
):
    events: list[str] = []
    monkeypatch.setattr(script_align, "scenes_from_script", lambda *_args: None)

    def fail_embedding(*_args, **_kwargs):
        events.append("embed")
        raise RuntimeError("embedding response failed")

    monkeypatch.setattr(
        pipeline_orch.scene_tools, "build_scenes_by_embedding", fail_embedding,
    )
    monkeypatch.setattr(
        pipeline_orch,
        "_maybe_unload_lm",
        lambda _settings, _reason, _log, model=None: events.append(f"unload:{model}"),
    )
    monkeypatch.setattr(
        pipeline_orch,
        "_ensure_lm_context",
        lambda *_args: events.append("load:chat"),
    )
    monkeypatch.setattr(
        pipeline_orch.scene_tools,
        "call_lm_studio_for_segments",
        lambda *_args, **_kwargs: events.append("group") or [_scene()],
    )

    pipeline_orch.run_scene_analysis(tmp_path, _segments(), _settings())

    assert events[:4] == ["embed", "unload:embedding-model", "load:chat", "group"]
