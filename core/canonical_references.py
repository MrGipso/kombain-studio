"""Durable canonical-image references for continuity-aware material runs.

The creative plan names a ``reference_frame_id``.  This module turns that name
into an execution artifact: one deterministic anchor card, one project-local
image and its SHA-256 digest.  Dependants always resolve the same anchor rather
than inheriting from the immediately preceding frame, which prevents gradual
visual drift across long sequences.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.project import atomic_write_text, is_within


REFERENCE_REGISTRY_FILE = ".canonical_references.json"
REFERENCE_REGISTRY_SCHEMA_VERSION = 1
_IMAGE_ROUTES = {"ai_image", "still_kenburns", "ai_image_then_i2v"}


def _existing_image(card: dict[str, Any]) -> Path | None:
    value = str(card.get("image_output", "") or "").strip()
    if not value:
        return None
    try:
        path = Path(value)
        return path if path.is_file() else None
    except (OSError, ValueError):
        return None


def build_reference_assignments(
    cards: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Map card ids to deterministic anchor/dependant reference roles."""

    groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, card in enumerate(cards):
        reference_id = str(card.get("reference_frame_id", "") or "").strip()
        route = str(card.get("visual_type", "") or "").strip().lower()
        card_id = str(card.get("card_id", "") or "").strip()
        if reference_id and card_id and route in _IMAGE_ROUTES:
            groups.setdefault(reference_id, []).append((index, card))

    assignments: dict[str, dict[str, Any]] = {}
    for reference_id, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(
            key=lambda item: (
                int(item[1].get("shot_index", 0) or 0),
                item[0],
            )
        )
        ready = [item for item in members if _existing_image(item[1]) is not None]
        anchor_index, anchor = (ready or members)[0]
        anchor_card_id = str(anchor.get("card_id", "") or "")
        anchor_shot_index = int(anchor.get("shot_index", 0) or 0)
        for card_index, card in members:
            card_id = str(card.get("card_id", "") or "")
            assignments[card_id] = {
                "reference_frame_id": reference_id,
                "reference_role": (
                    "anchor" if card_id == anchor_card_id else "dependent"
                ),
                "reference_anchor_card_id": anchor_card_id,
                "reference_anchor_shot_index": anchor_shot_index,
                "reference_anchor_card_index": anchor_index,
            }
    return assignments


def reference_plan(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one serializable anchor declaration per canonical reference."""

    assignments = build_reference_assignments(cards)
    by_reference: dict[str, dict[str, Any]] = {}
    for card_id, assignment in assignments.items():
        reference_id = assignment["reference_frame_id"]
        entry = by_reference.setdefault(
            reference_id,
            {
                "reference_frame_id": reference_id,
                "anchor_card_id": assignment["reference_anchor_card_id"],
                "anchor_shot_index": assignment["reference_anchor_shot_index"],
                "dependent_card_ids": [],
            },
        )
        if assignment["reference_role"] == "dependent":
            entry["dependent_card_ids"].append(card_id)
    return [by_reference[key] for key in sorted(by_reference)]


def _empty_registry() -> dict[str, Any]:
    return {
        "schema_version": REFERENCE_REGISTRY_SCHEMA_VERSION,
        "references": {},
    }


def load_reference_registry(project_dir: Path | str) -> dict[str, Any]:
    path = Path(project_dir) / REFERENCE_REGISTRY_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _empty_registry()
    if (
        not isinstance(data, dict)
        or int(data.get("schema_version", 0) or 0)
        != REFERENCE_REGISTRY_SCHEMA_VERSION
        or not isinstance(data.get("references"), dict)
    ):
        return _empty_registry()
    return data


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def register_reference(
    project_dir: Path | str,
    *,
    reference_frame_id: str,
    anchor_card_id: str,
    anchor_shot_index: int,
    image_path: Path | str,
) -> dict[str, Any]:
    """Atomically bind an id to one verified project-local anchor image."""

    root = Path(project_dir).resolve()
    image = Path(image_path)
    if not image.is_absolute():
        image = root / image
    image = image.resolve(strict=True)
    if not image.is_file() or not is_within(image, root):
        raise ValueError("Canonical reference image must be a project-local file")
    reference_id = str(reference_frame_id or "").strip()
    if not reference_id:
        raise ValueError("Canonical reference id is empty")

    entry = {
        "reference_frame_id": reference_id,
        "anchor_card_id": str(anchor_card_id or ""),
        "anchor_shot_index": int(anchor_shot_index),
        "image_path": image.relative_to(root).as_posix(),
        "image_sha256": _file_digest(image),
    }
    registry = load_reference_registry(root)
    registry["references"][reference_id] = entry
    atomic_write_text(
        root / REFERENCE_REGISTRY_FILE,
        json.dumps(registry, ensure_ascii=False, indent=2),
    )
    return entry


def resolve_reference(
    project_dir: Path | str,
    reference_frame_id: str,
    *,
    expected_anchor_card_id: str = "",
) -> Path | None:
    """Resolve a reference only when path, ownership and digest still match."""

    root = Path(project_dir).resolve()
    entry = load_reference_registry(root)["references"].get(
        str(reference_frame_id or "").strip()
    )
    if not isinstance(entry, dict):
        return None
    if expected_anchor_card_id and str(entry.get("anchor_card_id", "")) != str(
        expected_anchor_card_id
    ):
        return None
    raw_path = str(entry.get("image_path", "") or "").strip()
    expected_digest = str(entry.get("image_sha256", "") or "").strip()
    if not raw_path or not expected_digest:
        return None
    try:
        image = (root / raw_path).resolve(strict=True)
        if not image.is_file() or not is_within(image, root):
            return None
        if _file_digest(image) != expected_digest:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return image


__all__ = [
    "REFERENCE_REGISTRY_FILE",
    "build_reference_assignments",
    "load_reference_registry",
    "reference_plan",
    "register_reference",
    "resolve_reference",
]
