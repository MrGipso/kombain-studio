import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import content_pipeline_gui as cpg
from backend.routers import assets as assets_router
from core import asset_variants
from core.config import Settings
from core.models import AssetCard
from core.project import content_hash, create_project


def _card(**overrides) -> AssetCard:
    values = {
        "shot_index": 1,
        "scene_index": 1,
        "scene_title": "Scene",
        "start": "00:00:00",
        "end": "00:00:05",
        "visual_type": "ai_image_then_i2v",
        "route": "image+video",
        "description": "A server room",
        "asset_query": "server room",
        "image_model": "krea2",
        "video_model": "ltx2.3_i2v",
        "remotion_template": "",
        "image_prompt": "Wide server room, blue practical light",
        "video_prompt": "Slow dolly forward",
        "screen_text": "",
        "motion": "slow",
        "fallback": "still",
        "image_output": "",
        "video_output": "",
        "remotion_output": "",
        "status": "planned",
        "notes": "",
        "approved": False,
    }
    values.update(overrides)
    return AssetCard(**values)


def test_variants_snapshot_overwritten_outputs_and_keep_append_only_history(tmp_path):
    project = tmp_path / "project"
    source = project / "assets" / "videos" / "shot_001.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"first render")
    card = _card(video_output=str(source))
    settings = Settings(comfy_ltx_workflow="workflow.json")

    first = asset_variants.record_card_outputs(
        project,
        card_index=0,
        card=card,
        source="ai",
        mode="video",
        settings=settings,
        output_fields=["video_output"],
        force=True,
    )[0]
    first_snapshot = Path(first["file_path"])
    assert first_snapshot.read_bytes() == b"first render"
    assert first["file_hash"] == content_hash(b"first render")
    assert first["id"].startswith("var_")

    # The conventional render filename is reused by ComfyUI.  The old variant
    # remains byte-for-byte intact and the new record links to its parent.
    source.write_bytes(b"second render")
    second = asset_variants.record_card_outputs(
        project,
        card_index=0,
        card=card,
        source="ai",
        mode="video",
        settings=settings,
        output_fields=["video_output"],
        force=True,
    )[0]
    assert first_snapshot.read_bytes() == b"first render"
    assert second["parent_id"] == first["id"]
    assert second["id"] != first["id"]

    before_select = asset_variants.load_manifest(project)
    asset_variants.select_variant(project, first["id"], reason="test")
    after_select = asset_variants.load_manifest(project)
    assert after_select["variants"] == before_select["variants"]
    assert len(after_select["selection_events"]) == len(before_select["selection_events"]) + 1
    listed = asset_variants.list_variants(project, card_index=0)
    assert {item["id"]: item["selected"] for item in listed} == {
        first["id"]: True,
        second["id"]: False,
    }


def test_variant_captures_prompt_models_workflow_and_deduplicates_board_save(tmp_path):
    project = tmp_path / "project"
    image = project / "assets" / "images" / "shot_001.png"
    workflow = tmp_path / "krea.json"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"PNG-ish")
    workflow.write_text('{"1":{"class_type":"Sampler"}}', encoding="utf-8")
    card = _card(image_output=str(image), reference_query="Nomic AI")
    settings = SimpleNamespace(comfy_krea_workflow=str(workflow), api_secret="not persisted")

    records = asset_variants.record_card_outputs(
        project,
        card_index=0,
        card=card,
        source="manual",
        mode="card_update",
        settings=settings,
        output_fields=["image_output"],
    )
    assert len(records) == 1
    record = records[0]
    assert record["prompt"] == card.image_prompt
    assert record["query"] == "Nomic AI"
    assert record["model"] == card.image_model
    assert record["model_hash"] == content_hash(card.image_model)
    assert record["workflow"] == str(workflow)
    assert record["workflow_hash"] == content_hash(workflow)
    assert record["settings_hash"]
    assert "api_secret" not in json.dumps(record)

    # An ordinary Save with unchanged media must not manufacture a new version.
    assert asset_variants.record_card_outputs(
        project,
        card_index=0,
        card=card,
        source="manual",
        mode="card_update",
        settings=settings,
        output_fields=["image_output"],
    ) == []
    assert len(asset_variants.list_variants(project)) == 1


def test_variant_integrity_failure_blocks_selection(tmp_path):
    project = tmp_path / "project"
    media = project / "clip.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"valid")
    variant = asset_variants.record_variant(
        project,
        card_index=0,
        shot_index=1,
        source="manual",
        mode="assign",
        output_field="assigned_media",
        file_path=media,
    )
    Path(variant["file_path"]).write_bytes(b"tampered")

    try:
        asset_variants.select_variant(project, variant["id"])
    except asset_variants.VariantIntegrityError:
        pass
    else:
        raise AssertionError("tampered variant was selectable")


def test_asset_variant_api_lists_and_selects_snapshot(tmp_path, monkeypatch):
    project = create_project("Variants", tmp_path, settings=Settings(output_base=str(tmp_path)))
    first_file = project.assets_videos_dir / "first.mp4"
    second_file = project.assets_videos_dir / "second.mp4"
    first_file.write_bytes(b"first")
    second_file.write_bytes(b"second")
    card = _card(video_output=str(first_file), approved=True, status="approved")
    cpg.write_asset_board(project.dir, [card])
    first = asset_variants.record_card_outputs(
        project.dir,
        card_index=0,
        card=card,
        source="ai",
        mode="video",
        settings=project.settings,
        output_fields=["video_output"],
        force=True,
    )[0]
    card.video_output = str(second_file)
    second = asset_variants.record_card_outputs(
        project.dir,
        card_index=0,
        card=card,
        source="ai",
        mode="video",
        settings=project.settings,
        output_fields=["video_output"],
        force=True,
    )[0]
    cpg.write_asset_board(project.dir, [card])
    monkeypatch.setattr(assets_router, "_resolve_project", lambda _project_id: project.dir)

    payload = asyncio.run(assets_router.get_card_variants(project.dir.name, 0))
    assert payload["selections"]["video_output"] == second["id"]
    assert len(payload["variants"]) == 2

    selected = asyncio.run(
        assets_router.select_card_variant(project.dir.name, 0, first["id"])
    )
    assert selected["ok"] is True
    assert selected["variant"]["selected"] is True
    assert selected["card"]["video_output"] == first["file_path"]
    assert selected["card"]["approved"] is False
    assert cpg.load_asset_board(project.dir)[0].video_output == first["file_path"]


def test_manual_card_media_update_is_recorded(tmp_path, monkeypatch):
    project = create_project("Manual", tmp_path, settings=Settings(output_base=str(tmp_path)))
    card = _card()
    assigned = project.assets_images_dir / "manual.png"
    assigned.write_bytes(b"manual image")
    cpg.write_asset_board(project.dir, [card])
    monkeypatch.setattr(assets_router, "_resolve_project", lambda _project_id: project.dir)

    updated = asyncio.run(
        assets_router.update_card(
            project.dir.name,
            0,
            {"assigned_media": str(assigned)},
        )
    )

    assert updated["assigned_media"] == str(assigned)
    variants = asset_variants.list_variants(project.dir, card_index=0)
    assert len(variants) == 1
    assert variants[0]["source"] == "manual"
    assert variants[0]["mode"] == "card_update"
    assert variants[0]["output_field"] == "assigned_media"
    assert variants[0]["selected"] is True


def test_legacy_board_card_id_is_stable_and_persisted_on_next_write(tmp_path):
    project = tmp_path / "legacy"
    project.mkdir()
    legacy = _card().__dict__
    legacy.pop("card_id")
    board_path = project / "06_asset_board.json"
    board_path.write_text(json.dumps([legacy]), encoding="utf-8")

    first = cpg.load_asset_board(project)
    second = cpg.load_asset_board(project)
    assert first[0].card_id == second[0].card_id
    uuid.UUID(first[0].card_id)
    assert "card_id" not in json.loads(board_path.read_text(encoding="utf-8"))[0]

    cpg.write_asset_board(project, first)
    stored = json.loads(board_path.read_text(encoding="utf-8"))
    assert stored[0]["card_id"] == first[0].card_id


def test_variant_api_tracks_card_id_after_reorder_and_insert(tmp_path, monkeypatch):
    project = create_project("Stable ids", tmp_path, settings=Settings(output_base=str(tmp_path)))
    first_file = project.assets_videos_dir / "first-card.mp4"
    second_file = project.assets_videos_dir / "second-card.mp4"
    first_file.write_bytes(b"first card")
    second_file.write_bytes(b"second card")
    first_card = _card(shot_index=1, video_output=str(first_file))
    second_card = _card(shot_index=2, video_output=str(second_file))
    cpg.write_asset_board(project.dir, [first_card, second_card])
    first_variant = asset_variants.record_card_outputs(
        project.dir,
        card_index=0,
        card=first_card,
        source="ai",
        mode="video",
        output_fields=["video_output"],
        force=True,
    )[0]
    second_variant = asset_variants.record_card_outputs(
        project.dir,
        card_index=1,
        card=second_card,
        source="ai",
        mode="video",
        output_fields=["video_output"],
        force=True,
    )[0]

    inserted = _card(shot_index=99)
    cpg.write_asset_board(project.dir, [inserted, second_card, first_card])
    monkeypatch.setattr(assets_router, "_resolve_project", lambda _project_id: project.dir)

    second_history = asyncio.run(assets_router.get_card_variants(project.dir.name, 1))
    assert second_history["card_id"] == second_card.card_id
    assert [item["id"] for item in second_history["variants"]] == [second_variant["id"]]

    first_history = asyncio.run(assets_router.get_card_variants(project.dir.name, 2))
    assert first_history["card_id"] == first_card.card_id
    assert [item["id"] for item in first_history["variants"]] == [first_variant["id"]]

    with pytest.raises(HTTPException) as wrong_card:
        asyncio.run(
            assets_router.select_card_variant(
                project.dir.name, 1, first_variant["id"]
            )
        )
    assert wrong_card.value.status_code == 404

    selected = asyncio.run(
        assets_router.select_card_variant(
            project.dir.name, 1, second_variant["id"]
        )
    )
    assert selected["card"]["card_id"] == second_card.card_id
    assert selected["variant"]["card_id"] == second_card.card_id


def test_schema_v1_manifest_migrates_non_destructively_before_reorder(tmp_path):
    project = tmp_path / "migration"
    project.mkdir()
    card = _card()
    legacy_card = dict(card.__dict__)
    legacy_card.pop("card_id")
    (project / "06_asset_board.json").write_text(
        json.dumps([legacy_card]), encoding="utf-8"
    )
    legacy_manifest = {
        "schema_version": 1,
        "revision": 7,
        "created_at": "old",
        "updated_at": "old",
        "custom_key": {"preserve": True},
        "variants": [{"id": "var_old", "card_index": 0, "shot_index": 1}],
        "selection_events": [{
            "id": "sel_old",
            "card_index": 0,
            "output_field": "video_output",
            "variant_id": "var_old",
        }],
    }
    asset_variants.manifest_path(project).write_text(
        json.dumps(legacy_manifest), encoding="utf-8"
    )

    loaded = cpg.load_asset_board(project)
    inserted = _card(shot_index=99)
    cpg.write_asset_board(project, [inserted, loaded[0]])

    migrated = asset_variants.load_manifest(project)
    assert migrated["schema_version"] == 2
    assert migrated["custom_key"] == {"preserve": True}
    assert migrated["variants"][0]["id"] == "var_old"
    assert migrated["variants"][0]["card_index"] == 0
    assert migrated["variants"][0]["card_id"] == loaded[0].card_id
    assert migrated["selection_events"][0]["card_id"] == loaded[0].card_id
    assert asset_variants.card_variant_payload(
        project, 1, card_id=loaded[0].card_id, shot_index=1
    )["variants"][0]["id"] == "var_old"
