from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.routers.export_router import evaluate_export_gate
from core import asset_variants, export_manifest


def _card(media: Path, *, card_id: str = "card-stable-1") -> dict:
    return {
        "card_id": card_id,
        "shot_index": 1,
        "status": "ready",
        "approved": True,
        "route": "image",
        "visual_type": "ai_image",
        "image_output": str(media),
        "video_output": "",
        "assigned_media": "",
        "ltx_draft": False,
    }


def _record_selected(project: Path, card: dict) -> dict:
    return asset_variants.record_variant(
        project,
        card_index=0,
        card_id=card["card_id"],
        shot_index=card["shot_index"],
        source="test",
        mode="image",
        output_field="image_output",
        file_path=card["image_output"],
    )


def test_master_requires_explicit_provenance_but_draft_remains_permissive(tmp_path: Path):
    media = tmp_path / "shot.png"
    media.write_bytes(b"frame")
    card = _card(media)

    provenance = export_manifest.evaluate_master_provenance(tmp_path, [card])
    assert provenance["ok"] is False
    assert [reason["code"] for reason in provenance["reasons"]] == ["missing_provenance"]

    master = evaluate_export_gate(
        [card], {"issues": [], "summary": {"counts": {"error": 0}}},
        "master", provenance_report=provenance,
    )
    assert master.allowed is False
    assert any(reason.code == "missing_provenance" for reason in master.reasons)

    draft = evaluate_export_gate(
        [card], {"issues": [{"level": "error"}]}, "draft",
        provenance_report=provenance,
    )
    assert draft.allowed is True


def test_master_binds_actual_exported_bytes_to_selected_stable_card_variant(tmp_path: Path):
    media = tmp_path / "shot.png"
    media.write_bytes(b"same immutable frame")
    card = _card(media)
    variant = _record_selected(tmp_path, card)

    report = export_manifest.evaluate_master_provenance(tmp_path, [card])

    assert report["ok"] is True
    assert report["reasons"] == []
    assert report["cards"][0]["card_id"] == card["card_id"]
    assert report["cards"][0]["output_field"] == "image_output"
    assert report["cards"][0]["variant"]["id"] == variant["id"]
    assert report["cards"][0]["media_sha256"] == variant["file_hash"]


def test_stock_rights_survive_variant_and_master_sidecar_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    media = tmp_path / "stock.mp4"
    media.write_bytes(b"licensed stock bytes")
    card = {
        **_card(media),
        "route": "stock",
        "visual_type": "stock_video",
        "image_output": "",
        "video_output": str(media),
    }
    rights = {
        "source_url": "https://example.test/videos/42",
        "source_license": "Pexels License",
        "source_author": "Example Author",
    }
    asset_variants.record_variant(
        tmp_path,
        card_index=0,
        card_id=card["card_id"],
        shot_index=card["shot_index"],
        source="stock",
        mode="video",
        output_field="video_output",
        file_path=media,
        metadata=rights,
    )

    provenance = export_manifest.evaluate_master_provenance(tmp_path, [card])

    assert provenance["ok"] is True
    assert provenance["cards"][0]["variant"]["rights"] == {
        "source_url": rights["source_url"],
        "license": rights["source_license"],
        "author": rights["source_author"],
    }

    output = tmp_path / "master_stock.mp4"
    output.write_bytes(b"rendered licensed master")
    monkeypatch.setattr(
        export_manifest,
        "tool_contract",
        lambda _ffmpeg: {"application": "pipeline-studio", "ffmpeg": {"version": "test"}},
    )
    result = export_manifest.write_master_export_manifest(
        output,
        project_id="stock-project",
        project_name="Licensed Stock",
        project_schema_version=1,
        provenance=provenance,
        profile={"name": "master"},
        settings_contract={},
        technical_qc={"ok": True},
    )
    payload = json.loads((output.parent / result["path"]).read_text(encoding="utf-8"))
    assert payload["provenance"]["cards"][0]["variant"]["rights"] == {
        "source_url": rights["source_url"],
        "license": rights["source_license"],
        "author": rights["source_author"],
    }


def test_master_blocks_stock_without_source_url_and_license(tmp_path: Path):
    media = tmp_path / "unlicensed-stock.mp4"
    media.write_bytes(b"stock bytes without a rights record")
    card = {
        **_card(media),
        "route": "stock",
        "visual_type": "stock_video",
        "image_output": "",
        "video_output": str(media),
    }
    asset_variants.record_variant(
        tmp_path,
        card_index=0,
        card_id=card["card_id"],
        shot_index=card["shot_index"],
        source="stock",
        mode="video",
        output_field="video_output",
        file_path=media,
    )

    report = export_manifest.evaluate_master_provenance(tmp_path, [card])

    assert report["ok"] is False
    assert {reason["code"] for reason in report["reasons"]} == {
        "missing_stock_source_url",
        "missing_stock_license",
    }
    assert report["cards"] == []


def test_stock_rights_cannot_be_forged_by_editing_card_or_route(tmp_path: Path):
    media = tmp_path / "stock-disguised.png"
    media.write_bytes(b"stock bytes")
    card = _card(media)
    card.update({
        "route": "image",
        "source_url": "https://edited.invalid/source",
        "source_license": "edited card claim",
    })
    asset_variants.record_variant(
        tmp_path,
        card_index=0,
        card_id=card["card_id"],
        shot_index=card["shot_index"],
        source="stock",
        mode="stock",
        output_field="image_output",
        file_path=media,
        metadata={},
    )

    report = export_manifest.evaluate_master_provenance(tmp_path, [card])

    assert report["ok"] is False
    assert {reason["code"] for reason in report["reasons"]} == {
        "missing_stock_source_url",
        "missing_stock_license",
    }


def test_master_blocks_current_media_that_differs_from_selected_variant(tmp_path: Path):
    media = tmp_path / "shot.png"
    media.write_bytes(b"approved frame")
    card = _card(media)
    variant = _record_selected(tmp_path, card)

    media.write_bytes(b"silently replaced frame")
    report = export_manifest.evaluate_master_provenance(tmp_path, [card])

    assert report["ok"] is False
    reason = report["reasons"][0]
    assert reason["code"] == "selected_media_mismatch"
    assert reason["details"]["variant_id"] == variant["id"]
    assert reason["details"]["expected_sha256"] != reason["details"]["actual_sha256"]


def test_master_blocks_corrupt_immutable_variant_snapshot(tmp_path: Path):
    media = tmp_path / "shot.png"
    media.write_bytes(b"approved frame")
    card = _card(media)
    variant = _record_selected(tmp_path, card)
    Path(variant["file_path"]).write_bytes(b"tampered history")

    report = export_manifest.evaluate_master_provenance(tmp_path, [card])

    assert report["ok"] is False
    assert report["reasons"][0]["code"] == "selected_variant_corrupt"


@pytest.mark.parametrize("collection", ["variants", "selection_events"])
def test_schema_v2_manifest_rejects_records_without_stable_card_id(
    tmp_path: Path, collection: str,
):
    media = tmp_path / "shot.png"
    media.write_bytes(b"approved frame")
    card = _card(media)
    _record_selected(tmp_path, card)
    manifest_path = asset_variants.manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    manifest[collection][0].pop("card_id")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = export_manifest.evaluate_master_provenance(tmp_path, [card])

    assert report["ok"] is False
    assert report["reasons"][0]["code"] == "corrupt_provenance_manifest"
    assert report["reasons"][0]["details"]["collection"] == collection


def test_master_blocks_current_media_outside_project(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    approved = project / "approved.png"
    approved.write_bytes(b"approved frame")
    card = _card(approved)
    _record_selected(project, card)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"approved frame")
    card["image_output"] = str(outside)

    report = export_manifest.evaluate_master_provenance(project, [card])

    assert report["ok"] is False
    assert report["reasons"][0]["code"] == "export_media_outside_project"


def test_master_blocks_variant_file_outside_immutable_store(tmp_path: Path):
    media = tmp_path / "approved.png"
    media.write_bytes(b"approved frame")
    card = _card(media)
    _record_selected(tmp_path, card)
    manifest_path = asset_variants.manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["variants"][0]["file_path"] = str(media)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = export_manifest.evaluate_master_provenance(tmp_path, [card])

    assert report["ok"] is False
    assert report["reasons"][0]["code"] == "selected_variant_outside_store"


def test_master_selection_is_resolved_by_card_id_after_reorder(tmp_path: Path):
    first_media = tmp_path / "first.png"
    second_media = tmp_path / "second.png"
    first_media.write_bytes(b"first")
    second_media.write_bytes(b"second")
    first = _card(first_media, card_id="card-first")
    second = _card(second_media, card_id="card-second")
    asset_variants.record_variant(
        tmp_path, card_index=0, card_id="card-first", shot_index=1,
        source="test", mode="image", output_field="image_output", file_path=first_media,
    )
    second_variant = asset_variants.record_variant(
        tmp_path, card_index=1, card_id="card-second", shot_index=1,
        source="test", mode="image", output_field="image_output", file_path=second_media,
    )

    report = export_manifest.evaluate_master_provenance(tmp_path, [second, first])

    assert report["ok"] is True
    assert report["cards"][0]["card_id"] == "card-second"
    assert report["cards"][0]["variant"]["id"] == second_variant["id"]


def test_successful_master_sidecar_is_hashed_secret_free_and_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    media = tmp_path / "shot.png"
    media.write_bytes(b"frame")
    card = _card(media)
    _record_selected(tmp_path, card)
    provenance = export_manifest.evaluate_master_provenance(tmp_path, [card])
    output = tmp_path / "master_001.mp4"
    output.write_bytes(b"rendered master")
    monkeypatch.setattr(
        export_manifest,
        "tool_contract",
        lambda _ffmpeg: {"application": "pipeline-studio", "ffmpeg": {"version": "test"}},
    )

    result = export_manifest.write_master_export_manifest(
        output,
        project_id="project-1",
        project_name="Project One",
        project_schema_version=1,
        provenance=provenance,
        profile={"name": "master", "width": 1920, "height": 1080, "fps": 25},
        settings_contract={"export_loudnorm": True, "pexels_api_key": "must-not-leak"},
        technical_qc={"ok": True, "errors": [], "warnings": []},
        ffmpeg="ffmpeg",
        created_at="2026-07-11T12:00:00.000Z",
    )

    sidecar = output.parent / result["path"]
    original_bytes = sidecar.read_bytes()
    payload = json.loads(original_bytes)
    assert sidecar.name == "master_001.mp4.manifest.json"
    assert result["sha256"] == export_manifest.content_hash(sidecar)
    assert result["output_sha256"] == export_manifest.content_hash(output)
    assert payload["created_at"] == "2026-07-11T12:00:00.000Z"
    assert payload["project"]["id"] == "project-1"
    assert payload["provenance"]["cards"][0]["variant"]["id"].startswith("var_")
    assert payload["export"]["technical_qc"]["ok"] is True
    assert "pexels_api_key" not in payload["export"]["settings"]
    assert "must-not-leak" not in sidecar.read_text(encoding="utf-8")

    with pytest.raises(export_manifest.ExportManifestExists):
        export_manifest.write_master_export_manifest(
            output,
            project_id="project-1",
            project_name="Project One",
            project_schema_version=1,
            provenance=provenance,
            profile={"name": "master"},
            settings_contract={},
            technical_qc={"ok": True},
            ffmpeg="ffmpeg",
        )
    assert sidecar.read_bytes() == original_bytes


def test_edit_contract_hashes_audio_and_freezes_timeline_and_overlay(tmp_path: Path):
    voice_dir = tmp_path / "voice"
    music_dir = tmp_path / "music"
    sfx_dir = tmp_path / "assets" / "sfx"
    voice_dir.mkdir()
    music_dir.mkdir()
    sfx_dir.mkdir(parents=True)
    voice = voice_dir / "narration.wav"
    music = music_dir / "bed.wav"
    sfx = sfx_dir / "whoosh.wav"
    voice.write_bytes(b"voice-v1")
    music.write_bytes(b"music-v1")
    sfx.write_bytes(b"sfx-v1")
    (tmp_path / ".project.json").write_text(
        json.dumps({"music_selected": music.name}), encoding="utf-8"
    )
    (music_dir / "tracks.json").write_text(
        json.dumps({music.name: {"trim_start": 1.25, "trim_end": 9.5}}),
        encoding="utf-8",
    )
    timeline = {
        "voice_start": "00:00:03",
        "transitions": [{"from": 1, "to": 2, "kind": "crossfade"}],
    }
    card = {
        "card_id": "card-audio-1",
        "shot_index": 7,
        "scene_index": 2,
        "status": "ready",
        "route": "image",
        "start": "00:00:10",
        "end": "00:00:15",
        "timeline_start": "00:00:11",
        "timeline_end": "00:00:16",
        "track": "V2",
        "sfx_output": str(sfx),
        "text_overlay_enabled": True,
        "text_overlay_text": "Locked title",
        "text_overlay_position": "custom",
        "text_overlay_style": "lower-third",
        "text_overlay_x": 123,
        "text_overlay_y": 456,
        "text_overlay_enter": "00:00:11.250",
        "text_overlay_exit": "00:00:14.750",
    }

    contract = export_manifest.build_edit_contract(
        tmp_path,
        [card],
        timeline_settings=timeline,
        project_metadata={
            "music_enabled": True,
            "music_volume_db": -18,
            "music_duck": True,
            "music_selected": music.name,
        },
        settings_contract={"sfx_enabled": True},
    )

    assert contract["timeline"] == timeline
    frozen_card = contract["cards"][0]
    assert frozen_card["card_id"] == card["card_id"]
    assert frozen_card["timeline_start"] == card["timeline_start"]
    assert frozen_card["timeline_end"] == card["timeline_end"]
    assert frozen_card["track"] == "V2"
    assert frozen_card["text_overlay"] == {
        "enabled": True,
        "text": "Locked title",
        "position": "custom",
        "style": "lower-third",
        "x": 123,
        "y": 456,
        "enter": "00:00:11.250",
        "exit": "00:00:14.750",
    }
    assert contract["audio"]["voice"] == {
        "path": "voice/narration.wav",
        "sha256": export_manifest.content_hash(voice),
        "size": len(b"voice-v1"),
    }
    assert contract["audio"]["music"] == {
        "path": "music/bed.wav",
        "sha256": export_manifest.content_hash(music),
        "size": len(b"music-v1"),
    }
    assert contract["audio"]["music_trim"] == {"trim_start": 1.25, "trim_end": 9.5}
    assert contract["audio"]["sfx"] == [{
        "card_id": "card-audio-1",
        "shot_index": 7,
        "path": "assets/sfx/whoosh.wav",
        "sha256": export_manifest.content_hash(sfx),
        "size": len(b"sfx-v1"),
    }]
