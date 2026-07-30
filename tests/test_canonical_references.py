from __future__ import annotations

import json
from pathlib import Path

from core import canonical_references
from core import comfy_generation
from core.config import Settings


def _workflow() -> dict:
    return {
        "load": {
            "class_type": "LoadImage",
            "inputs": {"image": "old.png"},
            "_meta": {"title": "ENGINE_REFERENCE_IMAGE"},
        },
        "prompt": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "old prompt"},
            "_meta": {"title": "ENGINE_PROMPT"},
        },
        "save": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "old", "images": ["decode", 0]},
            "_meta": {"title": "ENGINE_OUTPUT"},
        },
        "sampler": {
            "class_type": "KSampler",
            "inputs": {"seed": 1},
        },
    }


def test_reference_operations_use_one_anchor_and_explicit_dependency():
    cards = [
        {
            "card_id": "card-b",
            "shot_index": 2,
            "visual_type": "ai_image",
            "reference_frame_id": "office",
        },
        {
            "card_id": "card-a",
            "shot_index": 1,
            "visual_type": "ai_image",
            "reference_frame_id": "office",
        },
    ]

    assignments = canonical_references.build_reference_assignments(cards)

    assert assignments["card-a"]["reference_role"] == "anchor"
    assert assignments["card-b"]["reference_role"] == "dependent"
    assert assignments["card-b"]["reference_anchor_card_id"] == "card-a"


def test_reference_registry_rejects_tampered_anchor(tmp_path: Path):
    project = tmp_path / "project"
    image = project / "assets" / "images" / "anchor.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"original")

    canonical_references.register_reference(
        project,
        reference_frame_id="office",
        anchor_card_id="card-a",
        anchor_shot_index=1,
        image_path=image,
    )

    assert canonical_references.resolve_reference(project, "office") == image
    image.write_bytes(b"tampered")
    assert canonical_references.resolve_reference(project, "office") is None


def test_marker_based_reference_workflow_is_bound_without_node_ids(tmp_path: Path):
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(_workflow()), encoding="utf-8")
    settings = Settings(
        comfy_reference_workflow=str(path),
        reference_conditioning_mode="required",
    )

    status = comfy_generation.reference_workflow_status(settings)
    workflow = comfy_generation.prepare_reference_image_workflow(
        "same actor, new angle",
        "canonical.png",
        9,
        settings=settings,
    )

    assert status["ok"] and status["enabled"]
    assert workflow["load"]["inputs"]["image"] == "canonical.png"
    assert workflow["prompt"]["inputs"]["text"] == "same actor, new angle"
    assert workflow["save"]["inputs"]["filename_prefix"].endswith(
        "shot_009_reference"
    )
    assert workflow["sampler"]["inputs"]["seed"] != 1


def test_required_reference_mode_fails_static_contract_before_generation(tmp_path: Path):
    settings = Settings(
        comfy_reference_workflow=str(tmp_path / "missing.json"),
        reference_conditioning_mode="required",
    )

    status = comfy_generation.reference_workflow_status(settings)

    assert not status["ok"]
    assert not status["enabled"]
    assert "not found" in status["error"]
