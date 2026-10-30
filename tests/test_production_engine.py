import json
import threading
from dataclasses import asdict
from pathlib import Path

import pytest
from PIL import Image

from backend.services import material_engine
from core import board as board_store
from core import continuity
from core import production as production_core
from core.models import AssetCard, TranscriptSegment
from core.project import content_hash
from core.config import Settings
from core.production import (
    ProductionPolicy,
    annotate_shot_segments,
    enforce_shot_visual_anchors,
    freeze_material_plan,
    load_material_plan,
    load_material_run,
    new_material_run,
    preflight_material_plan,
    save_material_run,
    timecode_to_milliseconds,
    transition_material_run,
)


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _card(**updates) -> AssetCard:
    data = {
        "shot_index": 1,
        "scene_index": 1,
        "scene_title": "Локальный движок",
        "start": "00:00:00",
        "end": "00:00:05",
        "visual_type": "ai_image_then_i2v",
        "route": "image+video",
        "description": "Локальный компьютер выполняет задачу",
        "asset_query": "local computer processing task",
        "image_model": "krea",
        "video_model": "ltx",
        "remotion_template": "",
        "image_prompt": "A local computer on a desk",
        "video_prompt": "Slow camera push",
        "screen_text": "",
        "motion": "slow push",
        "fallback": "still",
        "image_output": "",
        "video_output": "",
        "remotion_output": "",
        "status": "planned",
        "notes": "",
        "approved": False,
    }
    data.update(updates)
    return AssetCard(**data)


def _project(root: Path, *, repeated=False, stale=False) -> AssetCard:
    root.mkdir()
    _write_json(
        root / "01_transcript_segments.json",
        [
            {"start": "00:00:00.250", "end": "00:00:02.500", "text": "GPT работает."},
            {"start": "00:00:02.500", "end": "00:00:05.750", "text": "Локально."},
        ],
    )
    _write_json(
        root / "02_scenes.json",
        [
            {
                "index": 1,
                "start": "00:00:00.250",
                "end": "00:00:05.750",
                "title": "Локальный движок",
                "voice_text": "GPT работает локально.",
                "visual_direction": "Компьютер на столе",
                "screen_text": "",
                "music_note": "",
                "retention_note": "",
            }
        ],
    )
    shots = [
        {
            "index": 1,
            "scene_index": 1,
            "start": "00:00:00.250",
            "end": "00:00:05.750",
            "visual_type": "ai_image_then_i2v",
            "description": "GPT на локальном компьютере",
            "asset_query": "GPT local computer",
            "image_model": "krea",
            "video_model": "ltx",
            "image_prompt": "",
            "video_prompt": "",
            "screen_text": "",
            "motion": "",
            "priority": "medium",
            "fallback": "",
        }
    ]
    card = _card(
        start="00:00:00.250",
        end="00:00:05.750",
        description="GPT на локальном компьютере",
        asset_query="GPT local computer",
    )
    cards = [asdict(card)]
    if repeated:
        shots.append({**shots[0], "index": 2, "start": "00:00:05.750", "end": "00:00:18.750"})
        second = _card(
            shot_index=2,
            start="00:00:05.750",
            end="00:00:18.750",
            description="Продолжение, другой ракурс",
            asset_query="GPT local computer",
        )
        cards.append(asdict(second))
        # Keep transcript coverage valid for the longer continuation.
        segments = json.loads((root / "01_transcript_segments.json").read_text("utf-8"))
        segments.append(
            {"start": "00:00:05.750", "end": "00:00:18.750", "text": "Продолжение."}
        )
        _write_json(root / "01_transcript_segments.json", segments)
        scenes = json.loads((root / "02_scenes.json").read_text("utf-8"))
        scenes[0]["end"] = "00:00:18.750"
        scenes[0]["voice_text"] += " Продолжение."
        _write_json(root / "02_scenes.json", scenes)
    _write_json(root / "04_shot_list.json", shots)
    _write_json(root / "06_asset_board.json", cards)
    status = "stale" if stale else "completed"
    _write_json(
        root / ".pipeline_manifest.json",
        {"stages": {"scenes": {"status": status}, "shots": {"status": status}}},
    )
    return card


def _add_context_bible(root: Path, *, conflicting_location: bool = False) -> None:
    scenes = json.loads((root / "02_scenes.json").read_text("utf-8"))
    shots = json.loads((root / "04_shot_list.json").read_text("utf-8"))
    cards = json.loads((root / "06_asset_board.json").read_text("utf-8"))
    plan = [
        {
            "scene_index": 1,
            "location": "Кабинет с локальным компьютером",
            "location_id": "location-local-office",
            "primary_subject": "Локальный компьютер",
            "subject_ids": "subject-local-computer",
            "story_thread": "Локальный запуск GPT",
            "continuity_mode": "continuous_action",
            "entry_state": "Компьютер ожидает задачу",
            "scene_event": "GPT запускается локально",
            "exit_state": "Локальная задача выполняется",
            "transition": "Стык по движению камеры",
        }
    ]
    for index, (shot, card) in enumerate(zip(shots, cards)):
        context = {
            "purpose": (
                "Установить место и субъект"
                if index == 0
                else "Показать результат действия"
            ),
            "continuity_mode": "continuous_action",
            "continuity_group_id": "scene-001-location-local-office",
            "location_id": (
                "location-conflict"
                if conflicting_location and index == 1
                else "location-local-office"
            ),
            "subject_ids": ["subject-local-computer"],
            "state_before": (
                "Компьютер ожидает задачу"
                if index == 0
                else "GPT запускается локально"
            ),
            "action": "GPT запускается локально",
            "state_after": (
                "GPT запускается локально"
                if index == 0
                else "Локальная задача выполняется"
            ),
            "shot_size": "wide" if index == 0 else "close",
            "camera_angle": "eye_level",
            "screen_direction": "neutral",
            "reference_frame_id": "reference-location-local-office",
            "transition_in": "Начало сцены" if index == 0 else "Стык по действию",
            "transition_out": "Продолжение" if index == 0 else "Стык по камере",
            "change_from_previous": (
                "Установление контекста"
                if index == 0
                else "Новая крупность и результат"
            ),
        }
        shot.update(context)
        card.update(context)
    _write_json(root / "03_global_visual_plan.json", plan)
    _write_json(root / "04_shot_list.json", shots)
    _write_json(root / "06_asset_board.json", cards)
    bible = continuity.build_context_bible(scenes, plan, shots)
    continuity.write_context_bible(root, bible)


def test_millisecond_timing_and_segment_ownership_are_not_truncated():
    assert timecode_to_milliseconds("00:00:01.275") == 1275
    shots = [{
        "index": 1,
        "scene_index": 2,
        "start": "00:00:00.500",
        "end": "00:00:02.250",
        "visual_concept_id": "",
    }]
    segments = [
        {"start": "00:00:00.250", "end": "00:00:01.000", "text": "A"},
        {"start": "00:00:01.000", "end": "00:00:03.000", "text": "B"},
    ]
    annotated = annotate_shot_segments(shots, segments)
    assert annotated[0]["segment_ids"] == [1, 2]
    assert annotated[0]["visual_concept_id"] == "scene-002-shot-001"


def test_segment_annotation_accepts_active_pipeline_dataclasses():
    shots = [{
        "index": 7,
        "scene_index": 3,
        "start": "00:00:10",
        "end": "00:00:15",
        "visual_concept_id": "",
    }]
    segments = [
        TranscriptSegment("00:00:09", "00:00:12", "First"),
        TranscriptSegment("00:00:12", "00:00:16", "Second"),
    ]

    annotated = annotate_shot_segments(shots, segments)

    assert annotated[0]["segment_ids"] == [1, 2]
    assert annotated[0]["visual_concept_id"] == "scene-003-shot-007"


def test_visual_anchor_enforcement_adds_numeric_overlay_and_named_fact():
    shots = [
        {
            "index": 1,
            "scene_index": 1,
            "start": "00:00:00",
            "end": "00:00:05",
            "description": "Корабль выходит в море.",
            "asset_query": "warship at sea",
            "image_prompt": "A warship at sea.",
            "screen_text": "",
            "reference_query": "",
        },
        {
            "index": 2,
            "scene_index": 1,
            "start": "00:00:05",
            "end": "00:00:10",
            "description": "Корпус испытывает нагрузку.",
            "asset_query": "ship hull stress",
            "image_prompt": "A stressed ship hull.",
            "screen_text": "Уже занято",
            "reference_query": "",
        },
    ]
    segments = [
        {"start": "00:00:00", "end": "00:00:05", "text": "В 1999 году вышел Torrens."},
        {"start": "00:00:05", "end": "00:00:10", "text": "Нагрузка достигла 3000 тонн."},
    ]

    changed = enforce_shot_visual_anchors(shots, segments)

    assert changed == 2
    assert shots[0]["screen_text"] == "1999"
    assert "Torrens" in shots[0]["description"]
    assert shots[1]["screen_text"] == "Уже занято"
    assert "3000" in shots[1]["description"]
    assert "3000" in shots[1]["image_prompt"]


def test_preflight_blocks_stale_plan_and_warns_about_long_repeat(tmp_path):
    root = tmp_path / "project"
    _project(root, repeated=True, stale=True)
    result = preflight_material_plan(root)
    assert not result.allowed
    codes = {issue.code for issue in result.issues}
    assert {"stale_scenes", "stale_shots", "long_repeated_visual_concept"} <= codes


def test_preflight_blocks_timeline_gaps_and_card_timing_drift(tmp_path):
    root = tmp_path / "project"
    _project(root)
    shots = json.loads((root / "04_shot_list.json").read_text("utf-8"))
    shots[0]["end"] = "00:00:05.000"
    _write_json(root / "04_shot_list.json", shots)

    result = preflight_material_plan(root)
    codes = {issue.code for issue in result.issues}

    assert not result.allowed
    assert {"shot_timeline_gap", "card_timing_mismatch"} <= codes


def test_preflight_blocks_scene_text_that_drifted_from_transcript(tmp_path):
    root = tmp_path / "project"
    _project(root)
    scenes = json.loads((root / "02_scenes.json").read_text("utf-8"))
    scenes[0]["voice_text"] = "LLM добавила фразу, которой не было."
    _write_json(root / "02_scenes.json", scenes)

    result = preflight_material_plan(root)

    assert not result.allowed
    assert "scene_voice_text_mismatch" in {
        issue.code for issue in result.issues
    }


def test_context_aware_preflight_accepts_fresh_causal_storyboard(tmp_path):
    root = tmp_path / "project"
    _project(root)
    _add_context_bible(root)

    result = preflight_material_plan(root)

    assert result.allowed
    assert "stale_context_bible" not in {
        issue.code for issue in result.issues
    }
    snapshot = freeze_material_plan(root, result, ProductionPolicy())
    request = snapshot["asset_requests"][0]
    assert "Storyboard continuity constraints:" in request["compiled_image_prompt"]
    assert "initial visible state:" in request["compiled_video_prompt"]


def test_required_reference_workflow_blocks_before_material_run(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    _project(root, repeated=True)
    _add_context_bible(root)
    monkeypatch.setattr(
        "core.config.load_settings",
        lambda: Settings(
            comfy_reference_workflow=str(tmp_path / "missing-reference.json"),
            reference_conditioning_mode="required",
        ),
    )

    result = preflight_material_plan(root)

    assert not result.allowed
    assert "reference_workflow_unavailable" in {
        issue.code for issue in result.issues
    }


def test_context_preflight_blocks_stale_bible_and_group_conflict(tmp_path):
    stale_root = tmp_path / "stale"
    _project(stale_root)
    _add_context_bible(stale_root)
    shots = json.loads((stale_root / "04_shot_list.json").read_text("utf-8"))
    shots[0]["action"] = "Контекст изменён после фиксации"
    _write_json(stale_root / "04_shot_list.json", shots)

    stale = preflight_material_plan(stale_root)
    assert "stale_context_bible" in {issue.code for issue in stale.issues}

    conflict_root = tmp_path / "conflict"
    _project(conflict_root, repeated=True)
    _add_context_bible(conflict_root, conflicting_location=True)

    conflict = preflight_material_plan(conflict_root)
    assert "continuity_group_conflict" in {
        issue.code for issue in conflict.issues
    }


def test_context_preflight_blocks_broken_continuous_action_state_chain(tmp_path):
    root = tmp_path / "project"
    _project(root, repeated=True)
    _add_context_bible(root)
    scenes = json.loads((root / "02_scenes.json").read_text("utf-8"))
    plan = json.loads((root / "03_global_visual_plan.json").read_text("utf-8"))
    shots = json.loads((root / "04_shot_list.json").read_text("utf-8"))
    cards = json.loads((root / "06_asset_board.json").read_text("utf-8"))
    shots[1]["state_before"] = "Противоречивое исходное состояние"
    cards[1]["state_before"] = shots[1]["state_before"]
    _write_json(root / "04_shot_list.json", shots)
    _write_json(root / "06_asset_board.json", cards)
    continuity.write_context_bible(
        root,
        continuity.build_context_bible(scenes, plan, shots),
    )

    result = preflight_material_plan(root)

    assert not result.allowed
    assert "broken_action_state_chain" in {
        issue.code for issue in result.issues
    }


def test_frozen_plan_has_explicit_semantic_and_edit_links(tmp_path):
    root = tmp_path / "project"
    _project(root)
    preflight = preflight_material_plan(root)
    snapshot = freeze_material_plan(root, preflight, ProductionPolicy())

    assert snapshot["visual_concepts"] == [
        {
            "id": "scene-001-shot-001",
            "scene_index": 1,
            "segment_ids": [1, 2],
            "description": "GPT на локальном компьютере",
            "required_visual_anchors": ["GPT"],
        }
    ]
    assert snapshot["asset_requests"][0]["visual_concept_id"] == "scene-001-shot-001"
    assert [
        (operation["mode"], operation["phase"])
        for operation in snapshot["operations"]
    ] == [("image", "images"), ("video", "videos")]
    assert snapshot["edit_spans"][0] == {
        "id": "span-001",
        "scene_index": 1,
        "shot_index": 1,
        "visual_concept_id": "scene-001-shot-001",
        "asset_request_id": f"asset-{snapshot['cards'][0]['card_id']}",
        "start_ms": 250,
        "end_ms": 5750,
    }


def test_frozen_plan_rejects_content_tampering(tmp_path):
    root = tmp_path / "project"
    _project(root)
    preflight = preflight_material_plan(root)
    freeze_material_plan(root, preflight, ProductionPolicy())
    path = root / ".material_plan.json"
    snapshot = json.loads(path.read_text("utf-8"))
    snapshot["cards"][0]["description"] = "tampered after approval"
    _write_json(path, snapshot)

    assert load_material_plan(root) is None


def test_legacy_frozen_plan_remains_valid_for_process_resume(tmp_path):
    root = tmp_path / "project"
    _project(root)
    preflight = preflight_material_plan(root)
    snapshot = freeze_material_plan(root, preflight, ProductionPolicy())
    snapshot["schema_version"] = 1
    for card in snapshot["cards"]:
        for key in (
            "purpose",
            "continuity_mode",
            "continuity_group_id",
            "location_id",
            "subject_ids",
            "state_before",
            "action",
            "state_after",
            "shot_size",
            "camera_angle",
            "screen_direction",
            "reference_frame_id",
            "transition_in",
            "transition_out",
            "change_from_previous",
        ):
            card.pop(key, None)
    snapshot["plan_hash"] = production_core._plan_hash(
        snapshot["scenes"],
        snapshot["shots"],
        snapshot["cards"],
        card_fields=production_core.MATERIAL_PLAN_V1_HASH_CARD_FIELDS,
    )
    snapshot.pop("snapshot_hash", None)
    snapshot["snapshot_hash"] = content_hash(snapshot)
    _write_json(root / ".material_plan.json", snapshot)

    loaded = load_material_plan(root)

    assert loaded is not None
    assert loaded["schema_version"] == 1
    assert production_core.material_plan_matches_project(root, loaded)


def test_visual_anchors_are_owned_by_the_best_matching_shot(tmp_path):
    root = tmp_path / "project"
    _project(root)
    segments = [
        {"start": "00:00:00.250", "end": "00:00:02.500", "text": "GPT работает."},
        {"start": "00:00:02.500", "end": "00:00:05.750", "text": "API локально."},
    ]
    shots = json.loads((root / "04_shot_list.json").read_text("utf-8"))
    shots[0]["end"] = "00:00:02.500"
    shots.append(
        {
            **shots[0],
            "index": 2,
            "start": "00:00:02.500",
            "end": "00:00:05.750",
            "description": "Локальный API",
            "asset_query": "local API",
        }
    )
    cards = json.loads((root / "06_asset_board.json").read_text("utf-8"))
    cards[0]["end"] = "00:00:02.500"
    cards.append(
        asdict(
            _card(
                shot_index=2,
                start="00:00:02.500",
                end="00:00:05.750",
                description="Локальный API",
                asset_query="local API",
            )
        )
    )
    scenes = json.loads((root / "02_scenes.json").read_text("utf-8"))
    scenes[0]["voice_text"] = "GPT работает. API локально."
    _write_json(root / "01_transcript_segments.json", segments)
    _write_json(root / "02_scenes.json", scenes)
    _write_json(root / "04_shot_list.json", shots)
    _write_json(root / "06_asset_board.json", cards)

    preflight = preflight_material_plan(root)
    snapshot = freeze_material_plan(root, preflight, ProductionPolicy())

    assert preflight.allowed
    assert [
        concept["required_visual_anchors"]
        for concept in snapshot["visual_concepts"]
    ] == [["GPT"], ["API"]]


def test_policy_rejects_unbounded_retry_counts():
    with pytest.raises(ValueError, match="between 1 and 5"):
        ProductionPolicy(max_image_attempts=0)
    with pytest.raises(ValueError, match="between 1 and 5"):
        ProductionPolicy(max_video_attempts=10)


def test_material_run_state_machine_rejects_reopening_terminal_run(tmp_path):
    root = tmp_path / "project"
    _project(root)
    preflight = preflight_material_plan(root)
    run = new_material_run(root, root.name, preflight, ProductionPolicy())
    transition_material_run(run, "running")
    transition_material_run(run, "succeeded")

    with pytest.raises(ValueError, match="succeeded -> running"):
        transition_material_run(run, "running")


def test_corrupt_primary_checkpoint_recovers_previous_atomic_backup(tmp_path):
    root = tmp_path / "project"
    _project(root)
    preflight = preflight_material_plan(root)
    run = new_material_run(root, root.name, preflight, ProductionPolicy())
    transition_material_run(run, "running")
    save_material_run(root, run)
    (root / ".material_run.json").write_text("{broken", encoding="utf-8")

    recovered = load_material_run(root)

    assert recovered is not None
    assert recovered["status"] == "queued"


def test_operation_plan_can_skip_or_replace_existing_ready_media(tmp_path):
    pending = _card(visual_type="ai_image", route="image")
    assert [
        (item["mode"], item["kind"])
        for item in material_engine._operation_plan([pending], skip_ready=True)
    ] == [("image", "image")]

    image = tmp_path / "ready.png"
    image.write_bytes(b"already ready")
    card = _card(visual_type="ai_image", route="image", image_output=str(image))

    assert material_engine._operation_plan([card], skip_ready=True) == []
    operations = material_engine._operation_plan([card], skip_ready=False)
    assert [(item["mode"], item["kind"]) for item in operations] == [
        ("image", "image")
    ]


def test_unattended_run_retries_and_finishes_without_browser_loop(tmp_path):
    root = tmp_path / "project"
    _project(root)
    calls = []

    def generate(project_dir, card_index, mode, *_args, **_kwargs):
        calls.append(mode)
        cards = board_store.load_asset_board(project_dir)
        card = cards[card_index]
        if mode == "image" and calls.count("image") == 1:
            raise RuntimeError("temporary image failure")
        if mode == "image":
            path = project_dir / "assets" / "images" / "shot.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"image")
            card.image_output = str(path)
            card.status = "image_ready"
        elif mode == "video":
            path = project_dir / "assets" / "videos" / "shot.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")
            card.video_output = str(path)
            card.status = "generated"
        board_store.write_asset_board(project_dir, cards)
        return {"ok": True, "card": card.__dict__}

    result = material_engine.run_unattended_materials(
        root,
        root.name,
        ProductionPolicy(technical_validation=False),
        threading.Event(),
        generate,
    )
    assert result["status"] == "succeeded"
    assert calls == ["image", "image", "video"]
    assert result["summary"]["retries"] == 1
    assert result["summary"]["fallbacks"] == 0
    assert load_material_run(root)["status"] == "succeeded"


def test_unattended_video_uses_full_quality_workflow(tmp_path):
    root = tmp_path / "project"
    _project(root)
    force_full_by_mode = {}

    def generate(project_dir, card_index, mode, _skip_stock, force_full, **_kwargs):
        force_full_by_mode[mode] = force_full
        cards = board_store.load_asset_board(project_dir)
        card = cards[card_index]
        folder = "images" if mode == "image" else "videos"
        output = project_dir / "assets" / folder / f"generated-{mode}.bin"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"media")
        if mode == "image":
            card.image_output = str(output)
        else:
            card.video_output = str(output)
        card.status = "generated"
        board_store.write_asset_board(project_dir, cards)
        return {"card": card.__dict__}

    result = material_engine.run_unattended_materials(
        root,
        root.name,
        ProductionPolicy(technical_validation=False),
        threading.Event(),
        generate,
    )

    assert result["status"] == "succeeded"
    assert force_full_by_mode == {"image": False, "video": True}


def test_unattended_continuity_group_passes_one_anchor_to_every_dependent(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    _project(root, repeated=True)
    _add_context_bible(root)
    workflow = tmp_path / "reference.json"
    _write_json(
        workflow,
        {
            "load": {
                "class_type": "LoadImage",
                "inputs": {"image": "old.png"},
                "_meta": {"title": "ENGINE_REFERENCE_IMAGE"},
            },
            "prompt": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "old"},
                "_meta": {"title": "ENGINE_PROMPT"},
            },
            "save": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": "old"},
                "_meta": {"title": "ENGINE_OUTPUT"},
            },
        },
    )
    configured = Settings(
        comfy_reference_workflow=str(workflow),
        reference_conditioning_mode="required",
    )
    monkeypatch.setattr("core.config.load_settings", lambda: configured)
    references_by_shot = {}

    def generate(project_dir, card_index, mode, *_args, **kwargs):
        cards = board_store.load_asset_board(project_dir)
        card = cards[card_index]
        if mode == "image":
            references_by_shot[card.shot_index] = kwargs.get("reference_image")
            output = project_dir / "assets" / "images" / f"shot-{card.shot_index}.png"
            output.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (32, 32), "navy").save(output)
            card.image_output = str(output)
        else:
            output = project_dir / "assets" / "videos" / f"shot-{card.shot_index}.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"video")
            card.video_output = str(output)
        card.status = "generated"
        board_store.write_asset_board(project_dir, cards)
        return {"card": card.__dict__}

    result = material_engine.run_unattended_materials(
        root,
        root.name,
        ProductionPolicy(technical_validation=False),
        threading.Event(),
        generate,
    )

    assert result["status"] == "succeeded"
    assert references_by_shot[1] is None
    assert references_by_shot[2] == root / "assets" / "images" / "shot-1.png"


def test_corrupt_crash_output_is_regenerated_instead_of_skipped(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    _project(root)
    policy = ProductionPolicy(max_video_attempts=1)
    preflight = preflight_material_plan(root)
    freeze_material_plan(root, preflight, policy)
    new_material_run(root, root.name, preflight, policy)

    corrupt = root / "assets" / "images" / "partial.png"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"not a png")
    cards = board_store.load_asset_board(root)
    cards[0].image_output = str(corrupt)
    board_store.write_asset_board(root, cards)
    calls = []

    def generate(project_dir, card_index, mode, *_args, **_kwargs):
        calls.append(mode)
        if mode == "video":
            raise RuntimeError("video provider unavailable")
        current = board_store.load_asset_board(project_dir)
        output = project_dir / "assets" / "images" / "recovered.png"
        Image.new("RGB", (64, 64), "navy").save(output)
        current[card_index].image_output = str(output)
        current[card_index].status = "generated"
        board_store.write_asset_board(project_dir, current)
        return {"ok": True, "card": current[card_index].__dict__}

    def fallback(project_dir, card_index, *, ffmpeg, reason):
        current = board_store.load_asset_board(project_dir)
        video = project_dir / "assets" / "videos" / "fallback.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"fallback video")
        current[card_index].video_output = str(video)
        current[card_index].status = "generated"
        board_store.write_asset_board(project_dir, current)
        return current[card_index]

    monkeypatch.setattr(material_engine, "_apply_kenburns_fallback", fallback)
    result = material_engine.run_unattended_materials(
        root,
        root.name,
        policy,
        threading.Event(),
        generate,
    )

    assert result["status"] == "succeeded"
    assert calls == ["image", "video"]


def test_retry_budget_survives_process_resume(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _project(root)
    policy = ProductionPolicy(technical_validation=False)
    preflight = preflight_material_plan(root)
    freeze_material_plan(root, preflight, policy)
    state = new_material_run(root, root.name, preflight, policy)
    card_id = board_store.load_asset_board(root)[0].card_id
    state["status"] = "interrupted"
    state["cards"][card_id] = {
        "card_id": card_id,
        "card_index": 0,
        "shot_index": 1,
        "status": "generating",
        "attempts": {"image": 1, "video": 0},
        "fallback": False,
        "error": "first process failed",
    }
    save_material_run(root, state)
    calls = []

    def generate(*_args, **_kwargs):
        calls.append("attempt")
        raise RuntimeError("second process failed")

    def fallback(project_dir, card_index, *, ffmpeg, reason):
        cards = board_store.load_asset_board(project_dir)
        card = cards[card_index]
        image = project_dir / "assets" / "images" / "resumed-fallback.png"
        video = project_dir / "assets" / "videos" / "resumed-fallback.mp4"
        image.parent.mkdir(parents=True, exist_ok=True)
        video.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"fallback image")
        video.write_bytes(b"fallback video")
        card.image_output = str(image)
        card.video_output = str(video)
        card.status = "generated"
        board_store.write_asset_board(project_dir, cards)
        return card

    monkeypatch.setattr(material_engine, "_apply_kenburns_fallback", fallback)
    result = material_engine.run_unattended_materials(
        root,
        root.name,
        policy,
        threading.Event(),
        generate,
    )

    assert result["status"] == "succeeded"
    assert calls == ["attempt"]
    assert result["cards"][card_id]["attempts"]["image"] == 2
    assert result["summary"]["fallbacks"] == 1


def test_unattended_run_pauses_on_systemic_dependency_outage(tmp_path):
    root = tmp_path / "project"
    _project(root)
    calls = []

    with pytest.raises(
        material_engine.MaterialDependencyUnavailable,
        match="ComfyUI недоступен",
    ):
        material_engine.run_unattended_materials(
            root,
            root.name,
            ProductionPolicy(technical_validation=False),
            threading.Event(),
            lambda *_args, **_kwargs: calls.append("generate"),
            service_check=lambda _service: False,
            dependency_wait_timeout=0,
        )

    assert calls == []
    state = load_material_run(root)
    assert state["status"] == "interrupted"
    assert state["summary"]["fallbacks"] == 0


def test_unattended_run_uses_safe_fallback_after_bounded_attempts(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _project(root)
    calls = []

    def generate(*_args, **_kwargs):
        calls.append("failed")
        raise RuntimeError("provider unavailable")

    def fallback(project_dir, card_index, *, ffmpeg, reason):
        cards = board_store.load_asset_board(project_dir)
        card = cards[card_index]
        image = project_dir / "assets" / "images" / "fallback.png"
        video = project_dir / "assets" / "videos" / "fallback.mp4"
        image.parent.mkdir(parents=True, exist_ok=True)
        video.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"fallback image")
        video.write_bytes(b"fallback video")
        card.image_output = str(image)
        card.video_output = str(video)
        card.visual_type = "still_kenburns"
        card.route = "kenburns"
        card.status = "generated"
        board_store.write_asset_board(project_dir, cards)
        return card

    monkeypatch.setattr(material_engine, "_apply_kenburns_fallback", fallback)
    result = material_engine.run_unattended_materials(
        root,
        root.name,
        ProductionPolicy(technical_validation=False),
        threading.Event(),
        generate,
    )
    assert result["status"] == "succeeded"
    assert len(calls) == 2
    assert result["summary"]["fallbacks"] == 1
    assert result["summary"]["cards_ready"] == 1


def test_systemic_provider_failure_stops_queue_without_mass_fallback(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    _project(root)
    calls = []

    def generate(*_args, **_kwargs):
        calls.append("attempt")
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(
        material_engine,
        "_apply_kenburns_fallback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("systemic failure must not create fallback")
        ),
    )

    with pytest.raises(
        material_engine.MaterialSystemicProviderError,
        match="без массового fallback",
    ):
        material_engine.run_unattended_materials(
            root,
            root.name,
            ProductionPolicy(technical_validation=False),
            threading.Event(),
            generate,
        )

    state = load_material_run(root)
    assert calls == ["attempt", "attempt"]
    assert state["status"] == "failed"
    assert state["summary"]["fallbacks"] == 0
    assert state["summary"]["failures"] == 1


def test_fallback_degrades_to_publishable_still_when_ffmpeg_is_unavailable(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    _project(root)

    def fail_kenburns(*_args, **_kwargs):
        raise RuntimeError("ffmpeg unavailable")

    monkeypatch.setattr(material_engine.kenburns, "build_kenburns_clip", fail_kenburns)
    card = material_engine._apply_kenburns_fallback(
        root,
        0,
        ffmpeg="missing-ffmpeg",
        reason="provider unavailable",
    )

    assert card.visual_type == "ai_image"
    assert card.route == "image"
    assert Path(card.image_output).is_file()
    assert not card.video_output
