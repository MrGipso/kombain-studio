from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from core import board
from core.models import AssetCard, Scene, Shot, legacy_asset_card_id
import core.project as project_module


def _card(**overrides) -> AssetCard:
    values = {
        "shot_index": 1,
        "scene_index": 1,
        "scene_title": "Opening",
        "start": "00:00:00",
        "end": "00:00:05",
        "visual_type": "ai_image_then_i2v",
        "route": "image+video",
        "description": "A wide documentary view of a working server room",
        "asset_query": "server room",
        "image_model": "krea2_turbo",
        "video_model": "ltx2.3_i2v",
        "remotion_template": "",
        "image_prompt": "Wide server room with blue practical light",
        "video_prompt": "The camera slowly moves between server racks",
        "screen_text": "GLOBAL NETWORK",
        "motion": "slow dolly",
        "fallback": "still image",
        "image_output": "",
        "video_output": "",
        "remotion_output": "",
        "status": "planned",
        "notes": "",
        "approved": False,
    }
    values.update(overrides)
    return AssetCard(**values)


def test_asset_board_round_trip_preserves_canonical_card(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    expected = _card(
        source_url="https://example.test/clip",
        source_license="CC BY 4.0",
        source_author="Example Author",
        qa_score=0.91,
        text_overlay_enabled=True,
    )

    board.write_asset_board(project_dir, [expected])
    loaded = board.load_asset_board(project_dir)

    assert len(loaded) == 1
    assert asdict(loaded[0]) == asdict(expected)
    assert json.loads((project_dir / board.ASSET_BOARD_FILE).read_text("utf-8"))[0][
        "card_id"
    ] == expected.card_id


def test_legacy_board_gets_stable_read_only_id_then_persists_it(tmp_path):
    project_dir = tmp_path / "legacy"
    project_dir.mkdir()
    legacy = asdict(_card())
    legacy.pop("card_id")
    for field in (
        "timeline_start",
        "timeline_end",
        "assigned_media",
        "track",
        "text_overlay_enabled",
        "text_overlay_text",
        "text_overlay_position",
        "text_overlay_style",
        "text_overlay_x",
        "text_overlay_y",
        "text_overlay_enter",
        "text_overlay_exit",
    ):
        legacy.pop(field)
    path = project_dir / board.ASSET_BOARD_FILE
    original = json.dumps([legacy], ensure_ascii=False)
    path.write_text(original, encoding="utf-8")

    first = board.load_asset_board(project_dir)[0]
    second = board.load_asset_board(project_dir)[0]

    expected_id = legacy_asset_card_id(project_dir, 0, first.shot_index)
    assert first.card_id == second.card_id == expected_id
    assert path.read_text(encoding="utf-8") == original
    assert first.track == "video_1"
    assert first.text_overlay_position == "bottom"

    board.write_asset_board(project_dir, [first])
    assert json.loads(path.read_text(encoding="utf-8"))[0]["card_id"] == expected_id


def test_write_replaces_only_duplicate_or_missing_card_ids(tmp_path):
    project_dir = tmp_path / "ids"
    project_dir.mkdir()
    retained_id = _card().card_id
    cards = [
        _card(card_id=retained_id, shot_index=1),
        _card(card_id=retained_id, shot_index=2),
        _card(card_id="", shot_index=3),
    ]

    board.write_asset_board(project_dir, cards)
    ids = [card.card_id for card in board.load_asset_board(project_dir)]

    assert ids[0] == retained_id
    assert len(ids) == len(set(ids)) == 3
    assert all(ids)
    assert [card.card_id for card in board.load_asset_board(project_dir)] == ids


def test_build_reconciles_shot_and_preserves_card_identity_and_user_state(tmp_path):
    project_dir = tmp_path / "rebuild"
    project_dir.mkdir()
    scene = Scene(
        index=1,
        start="00:00:00",
        end="00:00:06",
        title="Updated scene",
        voice_text="Voice",
        visual_direction="Servers",
        screen_text="NETWORK",
        music_note="",
        retention_note="",
    )
    (project_dir / "02_scenes.json").write_text(
        json.dumps([asdict(scene)], ensure_ascii=False),
        encoding="utf-8",
    )
    old = _card(
        approved=True,
        assigned_media="assets/videos/manual.mp4",
        source_license="CC BY 4.0",
        timeline_start="00:00:01",
        status="generated",
    )
    board.write_asset_board(project_dir, [old])
    shot = Shot(
        index=1,
        scene_index=1,
        start="00:00:00",
        end="00:00:06",
        visual_type="ai_image_then_i2v",
        description="A new wide documentary view inside the same server room",
        asset_query="new server room",
        image_model="krea2_turbo",
        video_model="ltx2.3_i2v",
        image_prompt="New concrete server room",
        video_prompt="Slow push forward through the racks",
        screen_text="NETWORK",
        motion="slow push",
        priority="high",
        fallback="still image",
    )

    rebuilt = board.build_asset_board(project_dir, shots=[shot])[0]

    assert rebuilt.card_id == old.card_id
    assert rebuilt.description == shot.description
    assert rebuilt.scene_title == "Updated scene"
    assert rebuilt.approved is True
    assert rebuilt.status == "generated"
    assert rebuilt.assigned_media == old.assigned_media
    assert rebuilt.timeline_start == old.timeline_start
    assert rebuilt.source_license == old.source_license


def test_atomic_write_failure_keeps_previous_board(tmp_path, monkeypatch):
    project_dir = tmp_path / "atomic"
    project_dir.mkdir()
    board.write_asset_board(project_dir, [_card(description="before")])
    path = project_dir / board.ASSET_BOARD_FILE
    original = path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("simulated interruption")

    monkeypatch.setattr(project_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        board.write_asset_board(project_dir, [_card(description="after")])

    assert path.read_bytes() == original
    assert not list(project_dir.glob(f"{board.ASSET_BOARD_FILE}.*.tmp"))


def test_gui_module_reexports_canonical_board_contract():
    import content_pipeline_gui as cpg

    assert cpg.load_asset_board is board.load_asset_board
    assert cpg.write_asset_board is board.write_asset_board
    assert cpg.build_asset_board is board.build_asset_board
    assert cpg.sync_asset_card_route is board.sync_asset_card_route
