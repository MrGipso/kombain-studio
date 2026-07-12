from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.routers import export_router
from backend.routers.export_router import evaluate_export_gate


def _validation(*issues: dict, summary_errors: int | None = None) -> dict:
    error_count = (
        summary_errors
        if summary_errors is not None
        else sum(1 for issue in issues if issue.get("level") == "error")
    )
    return {
        "project": "test",
        "summary": {
            "status": "error" if error_count else "ok",
            "counts": {"error": error_count, "warning": 0, "info": 0},
        },
        "issues": list(issues),
    }


def _image_card(media: Path, **overrides) -> dict:
    card = {
        "shot_index": 1,
        "route": "image",
        "visual_type": "ai_image",
        "image_output": str(media),
        "video_output": "",
        "assigned_media": "",
        "status": "generated",
        "approved": True,
        "ltx_draft": False,
    }
    card.update(overrides)
    return card


def test_master_allows_only_ready_approved_final_cards(tmp_path: Path):
    media = tmp_path / "shot.png"
    media.write_bytes(b"image")

    report = evaluate_export_gate([_image_card(media)], _validation(), "master")

    assert report.allowed is True
    assert report.reasons == []
    assert report.summary.cards_required == 1
    assert report.summary.cards_ready == 1
    assert report.summary.blockers == 0


def test_master_reference_card_uses_generated_video(tmp_path: Path):
    video = tmp_path / "shot_001_reference.mp4"
    video.write_bytes(b"generated reference clip")
    card = _image_card(
        tmp_path / "missing-reference-still.png",
        route="reference",
        visual_type="web_reference_card",
        video_output=str(video),
    )

    assert export_router._usable_card_media(card) == str(video)
    report = evaluate_export_gate([card], _validation(), "master")

    assert report.allowed is True
    assert report.summary.cards_ready == 1
    assert report.reasons == []


def test_master_legacy_reference_visual_type_overrides_stale_image_route(tmp_path: Path):
    video = tmp_path / "legacy_reference.mp4"
    video.write_bytes(b"generated reference clip")
    card = _image_card(
        tmp_path / "missing.png",
        route="image",
        visual_type="web_reference_card",
        video_output=str(video),
    )

    report = evaluate_export_gate([card], _validation(), "master")

    assert report.allowed is True
    assert report.summary.cards_ready == 1


def test_master_reports_every_card_and_validation_blocker(tmp_path: Path):
    missing = tmp_path / "missing.mp4"
    validation_issue = {
        "level": "error",
        "stage": "shots",
        "message": "Broken timing",
        "item": "shot 7",
        "details": {"start": 10, "end": 5},
    }
    card = _image_card(
        missing,
        shot_index=7,
        route="image+video",
        visual_type="ai_image_then_i2v",
        video_model="ltx",
        status="needs_review",
        approved=False,
        ltx_draft=True,
    )

    report = evaluate_export_gate([card], _validation(validation_issue), "master")

    assert report.allowed is False
    assert {reason.code for reason in report.reasons} == {
        "validation_error",
        "missing_media",
        "needs_review",
        "ltx_draft",
        "not_approved",
    }
    card_reasons = [reason for reason in report.reasons if reason.card_index is not None]
    assert all(reason.card_index == 0 and reason.shot_index == 7 for reason in card_reasons)
    assert report.summary.validation_errors == 1
    assert report.summary.cards_ready == 0
    assert report.summary.blockers == 5


def test_master_ignores_explicitly_excluded_cards(tmp_path: Path):
    missing = tmp_path / "missing.png"
    cards = [
        _image_card(missing, status="skipped", approved=False, ltx_draft=True),
        _image_card(missing, shot_index=2, status="rejected", approved=False),
    ]

    report = evaluate_export_gate(cards, _validation(), "master")

    assert report.allowed is False
    assert [reason.code for reason in report.reasons] == ["no_required_cards"]
    assert report.summary.cards_required == 0
    assert report.summary.cards_excluded == 2


def test_draft_preserves_permissive_legacy_export(tmp_path: Path):
    bad_card = _image_card(
        tmp_path / "missing.png",
        status="needs_review",
        approved=False,
        ltx_draft=True,
    )
    validation_issue = {"level": "error", "stage": "shots", "message": "Broken"}

    report = evaluate_export_gate([bad_card], _validation(validation_issue), "draft")

    assert report.allowed is True
    assert report.reasons == []
    assert report.summary.blockers == 0


def test_master_honours_summary_only_validation_errors(tmp_path: Path):
    media = tmp_path / "shot.png"
    media.write_bytes(b"image")

    report = evaluate_export_gate(
        [_image_card(media, status="approved", approved=True)],
        _validation(summary_errors=2),
        "master",
    )

    assert report.allowed is False
    assert [reason.code for reason in report.reasons] == [
        "validation_error",
        "validation_error",
    ]
    assert report.summary.validation_errors == 2


def test_master_blocks_stale_pipeline_stage(tmp_path: Path):
    media = tmp_path / "shot.png"
    media.write_bytes(b"image")
    states = {
        stage: {"status": "completed"}
        for stage in ("transcript", "scenes", "shots", "assets", "timeline")
    }
    states["shots"] = {"status": "stale", "invalidated_by": "scenes"}

    report = evaluate_export_gate(
        [_image_card(media)], _validation(), "master", stage_states=states,
    )

    assert report.allowed is False
    reason = next(reason for reason in report.reasons if reason.code == "pipeline_not_current")
    assert reason.item == "shots"
    assert reason.details == {"status": "stale", "invalidated_by": "scenes"}


def test_blocked_master_stream_never_starts_ffmpeg(tmp_path: Path, monkeypatch):
    class FakeProject:
        dir = tmp_path
        settings = SimpleNamespace(ffmpeg="ffmpeg")
        metadata = {}

        @staticmethod
        def read_json(key: str):
            if key == "asset_board":
                return [_image_card(tmp_path / "missing.png", approved=False)]
            if key == "timeline":
                return None
            raise AssertionError(key)

        @staticmethod
        def content_hash(value):
            return str(value)

        @staticmethod
        def mark_stage_completed(*_args, **_kwargs):
            return None

        @staticmethod
        def stage_states():
            return {
                stage: {"status": "completed"}
                for stage in ("transcript", "scenes", "shots", "assets", "timeline")
            }

    export_called = False

    def fail_if_exported(**_kwargs):
        nonlocal export_called
        export_called = True
        raise AssertionError("FFmpeg export must not start behind a blocked Master gate")

    monkeypatch.setattr(export_router, "load_settings", lambda: SimpleNamespace(output_base=""))
    monkeypatch.setattr(export_router, "safe_project_path", lambda *_args: tmp_path)
    monkeypatch.setattr(export_router, "open_project", lambda *_args: FakeProject())
    monkeypatch.setattr(export_router, "validate_project", lambda _project: _validation())
    monkeypatch.setattr(export_router, "write_validation_report", lambda _project: _validation())
    monkeypatch.setattr(export_router, "export_video", fail_if_exported)

    async def collect_stream() -> str:
        response = await export_router.export("test-project", "master")
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    payload = asyncio.run(collect_stream())

    assert export_called is False
    assert '"event": "gate"' in payload
    assert '"allowed": false' in payload
    assert '"code": "master_export_blocked"' in payload
