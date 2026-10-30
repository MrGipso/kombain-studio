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
        start="00:00:00",
        end="00:00:06",
        description="A new wide documentary view inside the same server room",
        asset_query="new server room",
        image_prompt="New concrete server room",
        video_prompt="Slow push forward through the racks",
        motion="slow push",
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


def test_build_invalidates_media_when_same_index_gets_a_different_plan(tmp_path):
    project_dir = tmp_path / "changed-plan"
    project_dir.mkdir()
    scene = Scene(
        index=1,
        start="00:00:00",
        end="00:00:05",
        title="Printing",
        voice_text="Printing rollers create new banknotes.",
        visual_direction="Printing press",
        screen_text="",
        music_note="",
        retention_note="",
    )
    (project_dir / "02_scenes.json").write_text(
        json.dumps([asdict(scene)], ensure_ascii=False),
        encoding="utf-8",
    )
    old = _card(
        description="Cars behind a security fence",
        asset_query="salvage yard fence",
        image_prompt="Damaged cars behind chain link",
        video_prompt="Camera pans over the fence",
        image_output=str(project_dir / "assets" / "images" / "shot_001.png"),
        video_output=str(project_dir / "assets" / "videos" / "shot_001.mp4"),
        assigned_media="assets/videos/manual-old.mp4",
        source_url="https://example.test/old",
        source_license="Old license",
        status="generated",
        approved=True,
        qa_score=0.95,
    )
    image_path = project_dir / "assets" / "images" / "shot_001.png"
    video_path = project_dir / "assets" / "videos" / "shot_001.mp4"
    image_path.parent.mkdir(parents=True)
    video_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"old image")
    video_path.write_bytes(b"old video")
    old.image_output = str(image_path)
    old.video_output = str(video_path)
    board.write_asset_board(project_dir, [old])
    shot = Shot(
        index=1,
        scene_index=1,
        start="00:00:00",
        end="00:00:05",
        visual_type="ai_image_then_i2v",
        description="Close-up of working printing press rollers",
        asset_query="printing press rollers",
        image_model="krea2_turbo",
        video_model="ltx2.3_i2v",
        image_prompt="Heavy steel printing rollers under factory light",
        video_prompt="The rollers rotate at high speed",
        screen_text="",
        motion="fast rotation",
        priority="high",
        fallback="still image",
    )

    rebuilt = board.build_asset_board(project_dir, shots=[shot])[0]

    assert rebuilt.card_id != old.card_id
    assert rebuilt.plan_fingerprint == board.media_plan_fingerprint(shot)
    assert rebuilt.description == shot.description
    assert rebuilt.image_prompt == shot.image_prompt
    assert rebuilt.video_prompt == shot.video_prompt
    assert rebuilt.route == "image+video"
    assert rebuilt.status == "planned"
    assert rebuilt.approved is False
    assert rebuilt.image_output == ""
    assert rebuilt.video_output == ""
    assert rebuilt.assigned_media == ""
    assert rebuilt.source_url == ""
    assert rebuilt.source_license == ""
    assert rebuilt.qa_score == -1.0


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

