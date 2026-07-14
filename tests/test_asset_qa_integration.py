from types import SimpleNamespace

from PIL import Image

from backend.routers import assets as assets_router
from core import asset_qa, asset_qa_store, lm


def _card(**overrides):
    values = {
        "card_id": "card-a",
        "shot_index": 1,
        "start": "00:00:00",
        "end": "00:00:04",
        "route": "image+video",
        "visual_type": "ai_image_then_i2v",
        "description": "A red rescue helicopter over snowy mountains",
        "image_prompt": "red rescue helicopter, snowy mountains",
        "screen_text": "",
        "image_output": "",
        "video_output": "",
        "status": "generated",
        "notes": "",
        "approved": False,
        "qa_score": 0.99,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _settings(**overrides):
    values = {
        "ai_frame_qa": True,
        "ai_qa_mode": "shadow",
        "ai_qa_threshold": 0.45,
        "lm_vision_model": "vision-model",
        "lm_url": "http://127.0.0.1:1234/v1",
        "lm_model": "text-model",
        "lm_auto_unload": False,
        "lm_unload_delay": "0",
        "ffmpeg": "ffmpeg",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _report(mode="shadow", recommendation="retry_image", kind="image"):
    return {
        "report_id": f"{kind}-report",
        "card_id": "card-a",
        "kind": kind,
        "mode": mode,
        "recommendation": recommendation,
        "summary": "Visible defect",
        "applied_action": "flagged_review" if mode == "review" else "none",
    }


def test_router_shadow_records_evidence_without_status_mutation(monkeypatch, tmp_path):
    image = tmp_path / "frame.png"
    image.write_bytes(b"frame")
    card = _card(image_output=str(image))
    monkeypatch.setattr(asset_qa, "inspect_image", lambda *args, **kwargs: _report())

    reports = assets_router._qa_generated_outputs(
        tmp_path, card, _settings(), image=True,
    )

    assert reports[0]["recommendation"] == "retry_image"
    assert card.status == "generated"
    assert card.notes == ""
    assert card.qa_score == -1.0


def test_router_review_flags_but_never_regenerates(monkeypatch, tmp_path):
    image = tmp_path / "frame.png"
    image.write_bytes(b"frame")
    card = _card(image_output=str(image))
    calls = 0

    def inspect(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _report(mode="review")

    monkeypatch.setattr(asset_qa, "inspect_image", inspect)
    reports = assets_router._qa_generated_outputs(
        tmp_path, card, _settings(ai_qa_mode="review"), image=True,
    )

    assert calls == 1
    assert reports[0]["recommendation"] == "retry_image"
    assert card.status == "needs_review"
    assert "[QA:image-report]" in card.notes
    assert card.image_output == str(image)


def test_router_qa_outage_does_not_fail_successful_render(monkeypatch, tmp_path):
    image = tmp_path / "frame.png"
    image.write_bytes(b"frame")
    card = _card(image_output=str(image))

    def fail(*args, **kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(asset_qa, "inspect_image", fail)
    reports = assets_router._qa_generated_outputs(
        tmp_path, card, _settings(), image=True,
    )
    assert reports[0]["error"] == "audit store unavailable"
    assert card.status == "generated"


def test_image_service_persists_validated_hash_bound_observations(monkeypatch, tmp_path):
    image_path = tmp_path / "frame.png"
    Image.new("RGB", (32, 24), (220, 20, 20)).save(image_path)
    card = _card(image_output=str(image_path))
    observation = asset_qa.VisionObservations(
        semantic_alignment="match",
        composition="good",
        expected_text_legible="not_applicable",
        findings=[],
        confidence=0.96,
        summary="The requested helicopter is clearly visible.",
    )

    def structured(*args, **kwargs):
        assert kwargs["transport"] == "openai"
        assert kwargs["model"] == "vision-model"
        assert kwargs["image_bytes"].startswith(b"\x89PNG")
        assert kwargs["ttl"] == 20
        return lm.StructuredReply(value=observation, attempts=1, transport="openai")

    monkeypatch.setattr(lm, "chat_structured", structured)
    report = asset_qa.inspect_image(
        tmp_path, card, _settings(lm_auto_unload=True, lm_unload_delay="0")
    )

    assert report["recommendation"] == "accept"
    assert report["media"]["sha256"]
    assert report["vision"]["semantic_alignment"] == "match"
    assert report["attempts"] == 1
    stored = asset_qa_store.load_store(tmp_path)["reports"]
    assert stored == [report]
    assert "data:image" not in (tmp_path / asset_qa_store.ASSET_QA_FILE).read_text("utf-8")


def test_video_service_uses_deterministic_policy_only(monkeypatch, tmp_path):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")
    card = _card(video_output=str(video_path))
    monkeypatch.setattr(asset_qa, "observe_video", lambda *args, **kwargs: {
        "ok": True,
        "issues": [],
        "duration": 4.0,
        "black_fraction": 0.0,
        "frozen_fraction": 0.72,
    })
    report = asset_qa.inspect_video(tmp_path, card, _settings())
    assert report["recommendation"] == "retry_video"
    assert report["model"] == ""
    assert report["vision"] is None


def test_qa_refuses_media_outside_project(monkeypatch, tmp_path):
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"not inspected")
    card = _card(image_output=str(outside))
    called = False

    def observe(_path):
        nonlocal called
        called = True
        return {"ok": True, "issues": []}

    monkeypatch.setattr(asset_qa, "observe_image", observe)
    report = asset_qa.inspect_image(tmp_path, card, _settings())
    assert called is False
    assert report["recommendation"] == "retry_image"
    assert report["reason_codes"] == ["path_outside_project"]
