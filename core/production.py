"""UI-independent contracts and checkpoints for unattended material runs.

The production engine treats an approved shot list as an immutable plan and
records execution separately.  It intentionally does not import FastAPI or a
model provider: desktop, HTTP and future CLI clients share the same policy,
preflight and crash-recovery format.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from core import canonical_references, continuity
from core.project import atomic_write_text, content_hash


MATERIAL_RUN_FILE = ".material_run.json"
MATERIAL_RUN_BACKUP_FILE = ".material_run.backup.json"
MATERIAL_PLAN_FILE = ".material_plan.json"
MATERIAL_RUN_SCHEMA_VERSION = 1
MATERIAL_PLAN_SCHEMA_VERSION = 3
MATERIAL_PLAN_SUPPORTED_SCHEMA_VERSIONS = {1, 2, MATERIAL_PLAN_SCHEMA_VERSION}
MATERIAL_PLAN_CARD_FIELDS = (
    "card_id",
    "shot_index",
    "scene_index",
    "start",
    "end",
    "visual_type",
    "route",
    "description",
    "asset_query",
    "image_model",
    "video_model",
    "remotion_template",
    "image_prompt",
    "video_prompt",
    "screen_text",
    "motion",
    "fallback",
    "text_overlay_enabled",
    "text_overlay_text",
    "text_overlay_position",
    "text_overlay_style",
    "text_overlay_x",
    "text_overlay_y",
    "text_overlay_enter",
    "text_overlay_exit",
    "reference_query",
    "reference_source",
    "priority",
    "plan_fingerprint",
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
)
# Route/visual_type may be changed by an execution fallback. Their approved
# values remain integrity-protected in the frozen snapshot, while the live
# resume fingerprint stays stable across that engine-owned transition.
MATERIAL_PLAN_HASH_CARD_FIELDS = tuple(
    key for key in MATERIAL_PLAN_CARD_FIELDS if key not in {"visual_type", "route"}
)
# Version 1 predates Context Bible fields. Keep its exact fingerprint profile
# readable so an interrupted queue can survive an application upgrade without
# resetting retry budgets or discarding its frozen operation graph.
MATERIAL_PLAN_V1_HASH_CARD_FIELDS = tuple(
    key
    for key in MATERIAL_PLAN_HASH_CARD_FIELDS
    if key
    not in {
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
    }
)

MaterialRunStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
]
_MATERIAL_RUN_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "cancelled", "failed"},
    "running": {"interrupted", "succeeded", "failed", "cancelled"},
    "interrupted": {"running", "cancelled", "failed"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def timecode_to_milliseconds(value: Any) -> int:
    """Parse a timecode without the sub-second truncation used by legacy code."""

    text = str(value or "0").strip().replace(",", ".")
    parts = text.split(":")
    try:
        if len(parts) == 3:
            seconds = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            seconds = float(parts[0]) * 60 + float(parts[1])
        else:
            seconds = float(text or 0)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(seconds):
        return 0
    return max(0, round(seconds * 1000))


@dataclass(frozen=True)
class ProductionPolicy:
    """Bounded work policy for a run that must finish without user input."""

    mode: Literal["unattended"] = "unattended"
    max_image_attempts: int = 2
    max_video_attempts: int = 2
    fallback: Literal["kenburns"] = "kenburns"
    skip_ready: bool = True
    auto_resume: bool = True
    technical_validation: bool = True

    def __post_init__(self) -> None:
        if not 1 <= int(self.max_image_attempts) <= 5:
            raise ValueError("max_image_attempts must be between 1 and 5")
        if not 1 <= int(self.max_video_attempts) <= 5:
            raise ValueError("max_video_attempts must be between 1 and 5")

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProductionPolicy":
        raw = dict(data or {})
        allowed = {name for name in cls.__dataclass_fields__}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"Unknown production policy fields: {', '.join(unknown)}")
        return cls(**raw)


@dataclass(frozen=True)
class VisualConcept:
    """Creative intent independent from render cost and timeline reuse."""

    id: str
    scene_index: int
    segment_ids: list[int]
    description: str
    required_visual_anchors: list[str] = field(default_factory=list)
    purpose: str = ""
    continuity_mode: str = "independent"
    continuity_group_id: str = ""
    location_id: str = ""
    subject_ids: list[str] = field(default_factory=list)
    state_before: str = ""
    action: str = ""
    state_after: str = ""


@dataclass(frozen=True)
class AssetRequest:
    """One expensive provider request that may feed several edit spans."""

    id: str
    visual_concept_id: str
    route: str
    image_prompt: str = ""
    video_prompt: str = ""
    compiled_image_prompt: str = ""
    compiled_video_prompt: str = ""
    motion: str = ""
    reference_frame_id: str = ""


@dataclass(frozen=True)
class EditSpan:
    """Deterministic montage slot referencing an asset request."""

    id: str
    scene_index: int
    shot_index: int
    visual_concept_id: str
    asset_request_id: str
    start_ms: int
    end_ms: int


@dataclass
class PreflightIssue:
    code: str
    message: str
    level: Literal["blocker", "warning"] = "warning"
    scene_index: int | None = None
    shot_index: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class MaterialPreflight:
    allowed: bool
    plan_hash: str
    scenes: int
    shots: int
    cards: int
    segments: int
    pending_images: int
    pending_videos: int
    ready_cards: int
    excluded_cards: int
    required_services: list[str] = field(default_factory=list)
    issues: list[PreflightIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "blockers": sum(issue.level == "blocker" for issue in self.issues),
            "warnings": sum(issue.level == "warning" for issue in self.issues),
        }


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _media_exists(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        return Path(text).is_file()
    except (OSError, ValueError):
        return False


def card_needs_image(card: dict[str, Any]) -> bool:
    route = str(card.get("visual_type", "") or "").lower()
    if route in {"stock_video", "web_reference_card"}:
        return False
    return route in {"ai_image", "still_kenburns", "ai_image_then_i2v"} and not (
        _media_exists(card.get("image_output"))
    )


def card_needs_video(card: dict[str, Any]) -> bool:
    if str(card.get("status", "") or "").lower() == "skipped":
        return False
    route = str(card.get("visual_type", "") or "").lower()
    if route == "ai_image":
        return False
    return not (
        _media_exists(card.get("assigned_media"))
        or _media_exists(card.get("video_output"))
    )


def card_is_ready(card: dict[str, Any]) -> bool:
    if str(card.get("status", "") or "").lower() == "skipped":
        return True
    route = str(card.get("visual_type", "") or "").lower()
    if route == "ai_image":
        return _media_exists(card.get("assigned_media")) or _media_exists(
            card.get("image_output")
        )
    return _media_exists(card.get("assigned_media")) or _media_exists(
        card.get("video_output")
    )


def required_material_services(cards: list[dict[str, Any]]) -> list[str]:
    """Return external services required by the still-pending operations."""

    for card in cards:
        if str(card.get("status", "") or "").lower() == "skipped":
            continue
        visual_type = str(card.get("visual_type", "") or "").lower()
        needs_comfy_image = (
            visual_type in {"ai_image", "still_kenburns", "ai_image_then_i2v"}
            and card_needs_image(card)
        )
        needs_comfy_video = (
            visual_type == "ai_image_then_i2v" and card_needs_video(card)
        )
        if needs_comfy_image or needs_comfy_video:
            return ["comfy"]
    return []


def build_material_operations(
    cards: list[dict[str, Any]],
    *,
    skip_ready: bool = True,
) -> list[dict[str, Any]]:
    """Build the deterministic provider-grouped operation graph for a run."""

    operations: list[dict[str, Any]] = []
    active = [
        (index, card)
        for index, card in enumerate(cards)
        if str(card.get("status", "") or "").lower() != "skipped"
    ]

    def add(
        card_index: int,
        card: dict[str, Any],
        mode: str,
        kind: str,
        phase: str,
    ) -> None:
        operation = {
            "card_index": card_index,
            "card_id": str(card.get("card_id", "") or ""),
            "shot_index": int(card.get("shot_index", 0) or 0),
            "mode": mode,
            "kind": kind,
            "phase": phase,
        }
        operation["operation_id"] = f"{phase}:{operation['card_id']}"
        operation["depends_on"] = []
        operations.append(operation)

    for index, card in active:
        visual_type = str(card.get("visual_type", "") or "").lower()
        if (
            visual_type in {"web_reference_card", "stock_video"}
            and (not skip_ready or card_needs_video(card))
        ):
            add(index, card, "route", "video", "sources")

    for index, card in active:
        visual_type = str(card.get("visual_type", "") or "").lower()
        if visual_type in {"ai_image", "still_kenburns", "ai_image_then_i2v"}:
            if not skip_ready or card_needs_image(card):
                add(index, card, "image", "image", "images")

    for index, card in active:
        if (
            str(card.get("visual_type", "") or "").lower() == "still_kenburns"
            and (not skip_ready or card_needs_video(card))
        ):
            add(index, card, "route", "video", "kenburns")

    for index, card in active:
        if (
            str(card.get("visual_type", "") or "").lower() == "ai_image_then_i2v"
            and (not skip_ready or card_needs_video(card))
        ):
            add(index, card, "video", "video", "videos")

    planned_ids = {operation["card_id"] for operation in operations}
    for index, card in active:
        if str(card.get("card_id", "") or "") in planned_ids or (
            skip_ready and card_is_ready(card)
        ):
            continue
        add(index, card, "route", "video", "other")

    assignments = canonical_references.build_reference_assignments(cards)
    image_operation_by_card = {
        operation["card_id"]: operation
        for operation in operations
        if operation["phase"] == "images"
    }
    for operation in operations:
        assignment = assignments.get(operation["card_id"])
        if operation["phase"] == "images" and assignment:
            operation.update(assignment)
            anchor_operation = image_operation_by_card.get(
                assignment["reference_anchor_card_id"]
            )
            if (
                assignment["reference_role"] == "dependent"
                and anchor_operation is not None
            ):
                operation["depends_on"].append(anchor_operation["operation_id"])
        elif operation["phase"] in {"kenburns", "videos"}:
            image_operation = image_operation_by_card.get(operation["card_id"])
            if image_operation is not None:
                operation["depends_on"].append(image_operation["operation_id"])

    phase_order = {
        "sources": 0,
        "images": 1,
        "kenburns": 2,
        "videos": 3,
        "other": 4,
    }
    operations.sort(
        key=lambda operation: (
            phase_order.get(operation["phase"], 99),
            0 if operation.get("reference_role") == "anchor" else 1,
            int(operation.get("shot_index", 0) or 0),
            operation["operation_id"],
        )
    )
    return operations


def overlapping_segment_ids(
    shot: dict[str, Any],
    segments: list[dict[str, Any]],
) -> list[int]:
    """Return 1-based transcript segment IDs touched by a shot."""

    start = timecode_to_milliseconds(shot.get("start"))
    end = timecode_to_milliseconds(shot.get("end"))
    result: list[int] = []
    for index, segment in enumerate(segments, start=1):
        seg_start = timecode_to_milliseconds(segment.get("start"))
        seg_end = timecode_to_milliseconds(segment.get("end"))
        if seg_end > start and seg_start < end:
            result.append(index)
    return result


def annotate_shot_segments(
    shots: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add backward-compatible semantic/timeline identities to shot dictionaries."""

    result: list[dict[str, Any]] = []
    for shot in shots:
        item = dict(shot)
        item["segment_ids"] = overlapping_segment_ids(item, segments)
        if not str(item.get("visual_concept_id", "") or "").strip():
            item["visual_concept_id"] = (
                f"scene-{int(item.get('scene_index', 0) or 0):03d}-"
                f"shot-{int(item.get('index', 0) or 0):03d}"
            )
        result.append(item)
    return result


def _normalised_concept(card: dict[str, Any]) -> str:
    # Search/generation query is the strongest compact concept identity.  A
    # continuation often changes description prose while reusing exactly the
    # same underlying asset, which is the repetition we need to expose.
    query = str(card.get("asset_query", "") or "").strip()
    text = query or str(card.get("description", "") or "")
    text = re.sub(
        r"\((?:продолжение|continuation)[^)]*\)",
        "",
        text,
        flags=re.IGNORECASE,
    ).lower()
    return re.sub(r"\W+", " ", text, flags=re.UNICODE).strip()


_ANCHOR_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z][A-Za-z0-9.+-]{1,}(?:\s+[A-Za-z][A-Za-z0-9.+-]{1,})?|"
    r"\d+[A-Za-z][A-Za-z0-9]*|\d+(?:[.,-]\d+)*)"
)
_ANCHOR_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "for",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
}


def _visual_anchors(text: str) -> list[str]:
    anchors: list[str] = []
    for match in _ANCHOR_RE.finditer(str(text or "")):
        anchor = re.sub(r"\s+", " ", match.group(0)).strip()
        anchor = re.sub(r"[.,;:!?]+$", "", anchor)
        if anchor.casefold() in _ANCHOR_STOPWORDS:
            continue
        if anchor not in anchors:
            anchors.append(anchor)
    return anchors


def _contains_anchor(haystack: str, anchor: str) -> bool:
    compact = re.sub(r"[^a-zа-яё0-9]+", "", str(haystack).casefold())
    needle = re.sub(r"[^a-zа-яё0-9]+", "", str(anchor).casefold())
    return bool(needle and needle in compact)


def _shot_anchor_requirements(
    shots: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> dict[int, list[str]]:
    """Assign every factual visual anchor to one best-overlapping shot."""

    requirements: dict[int, list[str]] = {}
    for segment in segments:
        anchors = _visual_anchors(str(segment.get("text", "") or ""))
        if not anchors:
            continue
        segment_start = timecode_to_milliseconds(segment.get("start"))
        segment_end = timecode_to_milliseconds(segment.get("end"))
        candidates: list[tuple[int, int]] = []
        for shot in shots:
            shot_start = timecode_to_milliseconds(shot.get("start"))
            shot_end = timecode_to_milliseconds(shot.get("end"))
            overlap = min(segment_end, shot_end) - max(segment_start, shot_start)
            if overlap > 0:
                candidates.append((overlap, int(shot.get("index", 0) or 0)))
        if not candidates:
            continue
        # Longest semantic overlap owns the fact; ties go to the earlier shot.
        _overlap, owner = max(candidates, key=lambda item: (item[0], -item[1]))
        owned = requirements.setdefault(owner, [])
        for anchor in anchors:
            if anchor not in owned:
                owned.append(anchor)
    return requirements


def _plan_hash(
    scenes: list[dict[str, Any]],
    shots: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    *,
    card_fields: tuple[str, ...] = MATERIAL_PLAN_HASH_CARD_FIELDS,
) -> str:
    card_plan = [
        {
            key: card.get(key)
            for key in card_fields
        }
        for card in cards
    ]
    return content_hash({"scenes": scenes, "shots": shots, "cards": card_plan})


def preflight_material_plan(project_dir: Path | str) -> MaterialPreflight:
    """Validate only facts that unattended execution can act on reliably."""

    root = Path(project_dir)
    scenes = _read_json(root / "02_scenes.json", [])
    shots = _read_json(root / "04_shot_list.json", [])
    cards = _read_json(root / "06_asset_board.json", [])
    segments = _read_json(root / "01_transcript_segments.json", [])
    global_plan = _read_json(root / "03_global_visual_plan.json", [])
    manifest = _read_json(root / ".pipeline_manifest.json", {})
    issues: list[PreflightIssue] = []

    for label, items, code in (
        ("сцен", scenes, "missing_scenes"),
        ("кадров", shots, "missing_shots"),
        ("карточек материалов", cards, "missing_asset_board"),
        ("сегментов транскрипта", segments, "missing_segments"),
    ):
        if not isinstance(items, list) or not items:
            issues.append(
                PreflightIssue(
                    code=code,
                    level="blocker",
                    message=f"Нет {label}; unattended-запуск невозможен.",
                )
            )
    if not all(isinstance(items, list) for items in (scenes, shots, cards, segments)):
        scenes, shots, cards, segments = [], [], [], []

    context_fields = (
        "purpose",
        "continuity_group_id",
        "location_id",
        "state_before",
        "action",
        "state_after",
        "shot_size",
        "camera_angle",
        "screen_direction",
        "transition_in",
        "transition_out",
        "change_from_previous",
    )
    context_aware = any(
        any(str(shot.get(key, "") or "").strip() for key in context_fields)
        for shot in shots
        if isinstance(shot, dict)
    )
    context_path = root / continuity.CONTEXT_BIBLE_FILE
    context_bible = continuity.load_context_bible(root)
    if context_path.exists() and context_bible is None:
        issues.append(
            PreflightIssue(
                code="invalid_context_bible",
                level="blocker",
                message=(
                    "Контекстная библия повреждена или имеет неизвестную версию. "
                    "Заново соберите раскадровку."
                ),
            )
        )
    elif context_aware and context_bible is None:
        issues.append(
            PreflightIssue(
                code="missing_context_bible",
                level="blocker",
                message=(
                    "Контекстная раскадровка не зафиксирована. "
                    "Заново выполните анализ кадров."
                ),
            )
        )
    elif context_bible is not None:
        current_source_hash = continuity.context_source_hash(
            scenes,
            global_plan if isinstance(global_plan, list) else [],
            shots,
        )
        if context_bible.get("source_hash") != current_source_hash:
            issues.append(
                PreflightIssue(
                    code="stale_context_bible",
                    level="blocker",
                    message=(
                        "Сцены или контекст кадров изменились после создания "
                        "Context Bible. Пересоберите раскадровку."
                    ),
                )
            )

    continuity_groups: dict[str, list[dict[str, Any]]] = {}
    for shot in shots:
        shot_id = int(shot.get("index", 0) or 0)
        scene_id = int(shot.get("scene_index", 0) or 0)
        if context_aware:
            missing = [
                key
                for key in context_fields
                if not str(shot.get(key, "") or "").strip()
            ]
            if missing:
                issues.append(
                    PreflightIssue(
                        code="incomplete_shot_context",
                        level="blocker",
                        scene_index=scene_id,
                        shot_index=shot_id,
                        message=(
                            f"Кадр {shot_id} не имеет обязательного контекста: "
                            + ", ".join(missing)
                        ),
                        details={"fields": missing},
                    )
                )
        mode = continuity.normalize_continuity_mode(
            shot.get("continuity_mode")
        )
        group_id = str(shot.get("continuity_group_id", "") or "").strip()
        if mode != "independent" and not group_id:
            issues.append(
                PreflightIssue(
                    code="missing_continuity_group",
                    level="blocker",
                    scene_index=scene_id,
                    shot_index=shot_id,
                    message=(
                        f"Связанный кадр {shot_id} не включён в continuity-группу."
                    ),
                )
            )
        if group_id:
            continuity_groups.setdefault(group_id, []).append(shot)

    for group_id, group_shots in continuity_groups.items():
        modes = {
            continuity.normalize_continuity_mode(shot.get("continuity_mode"))
            for shot in group_shots
        }
        locations = {
            str(shot.get("location_id", "") or "").strip()
            for shot in group_shots
            if str(shot.get("location_id", "") or "").strip()
        }
        subject_sets = {
            tuple(sorted(continuity.normalize_subject_ids(shot.get("subject_ids"))))
            for shot in group_shots
        }
        group_mode = next(iter(modes), "independent")
        location_conflict = (
            group_mode in {"same_location", "continuous_action"}
            and len(locations) > 1
        )
        subject_conflict = (
            group_mode in {"recurring_subject", "continuous_action"}
            and len(subject_sets) > 1
        )
        if len(modes) > 1 or location_conflict or subject_conflict:
            issues.append(
                PreflightIssue(
                    code="continuity_group_conflict",
                    level="blocker",
                    shot_index=int(group_shots[0].get("index", 0) or 0),
                    message=(
                        f"Continuity-группа {group_id} противоречива: "
                        "режим, локация или субъекты меняются внутри группы."
                    ),
                    details={
                        "shot_indices": [
                            int(shot.get("index", 0) or 0)
                            for shot in group_shots
                        ]
                    },
                )
            )
        if group_mode == "continuous_action":
            ordered_group = sorted(
                group_shots,
                key=lambda shot: (
                    int(shot.get("scene_index", 0) or 0),
                    int(shot.get("index", 0) or 0),
                ),
            )
            broken_boundaries: list[dict[str, Any]] = []
            for previous, current in zip(ordered_group, ordered_group[1:]):
                previous_state = str(previous.get("state_after", "") or "").strip()
                current_state = str(current.get("state_before", "") or "").strip()
                if previous_state != current_state:
                    broken_boundaries.append(
                        {
                            "previous_shot": int(previous.get("index", 0) or 0),
                            "current_shot": int(current.get("index", 0) or 0),
                            "state_after": previous_state,
                            "state_before": current_state,
                        }
                    )
            if broken_boundaries:
                issues.append(
                    PreflightIssue(
                        code="broken_action_state_chain",
                        level="blocker",
                        shot_index=int(
                            broken_boundaries[0].get("current_shot", 0) or 0
                        ),
                        message=(
                            f"В непрерывном действии {group_id} нарушена цепочка "
                            "state_after → state_before."
                        ),
                        details={"boundaries": broken_boundaries},
                    )
                )
        directions = {
            str(shot.get("screen_direction", "") or "").strip()
            for shot in group_shots
        }
        if {"left_to_right", "right_to_left"} <= directions:
            issues.append(
                PreflightIssue(
                    code="screen_direction_flip",
                    level="warning",
                    shot_index=int(group_shots[0].get("index", 0) or 0),
                    message=(
                        f"В группе {group_id} меняется экранное направление. "
                        "Нужен нейтральный план или осознанное пересечение оси."
                    ),
                )
            )

    stages = manifest.get("stages", {}) if isinstance(manifest, dict) else {}
    for stage in ("scenes", "shots"):
        state = stages.get(stage, {}) if isinstance(stages, dict) else {}
        if isinstance(state, dict) and state.get("status") == "stale":
            issues.append(
                PreflightIssue(
                    code=f"stale_{stage}",
                    level="blocker",
                    message=(
                        f"Этап «{stage}» устарел после изменения настроек. "
                        "Сначала заново утвердите план."
                    ),
                )
            )

    timing_tolerance_ms = 100
    scene_id_list = [int(scene.get("index", 0) or 0) for scene in scenes]
    scene_ids = set(scene_id_list)
    duplicate_scene_ids = sorted(
        scene_id for scene_id in scene_ids if scene_id_list.count(scene_id) > 1
    )
    if duplicate_scene_ids:
        issues.append(
            PreflightIssue(
                code="duplicate_scene_ids",
                level="blocker",
                message="В плане повторяются идентификаторы сцен.",
                details={"scene_indices": duplicate_scene_ids},
            )
        )

    sorted_scenes = sorted(
        scenes,
        key=lambda scene: timecode_to_milliseconds(scene.get("start")),
    )
    scene_covered_segments: set[int] = set()
    previous_scene_end: int | None = None
    for scene in sorted_scenes:
        scene_id = int(scene.get("index", 0) or 0)
        scene_start = timecode_to_milliseconds(scene.get("start"))
        scene_end = timecode_to_milliseconds(scene.get("end"))
        if previous_scene_end is not None:
            delta = scene_start - previous_scene_end
            if delta > timing_tolerance_ms:
                issues.append(
                    PreflightIssue(
                        code="scene_timeline_gap",
                        level="blocker",
                        scene_index=scene_id,
                        message=f"Перед сценой {scene_id} есть разрыв {delta / 1000:.2f} с.",
                    )
                )
            elif delta < -timing_tolerance_ms:
                issues.append(
                    PreflightIssue(
                        code="scene_timeline_overlap",
                        level="blocker",
                        scene_index=scene_id,
                        message=f"Сцена {scene_id} накладывается на предыдущую на {-delta / 1000:.2f} с.",
                    )
                )
        previous_scene_end = max(previous_scene_end or 0, scene_end)
        segment_ids = overlapping_segment_ids(scene, segments)
        scene_covered_segments.update(segment_ids)
        expected_voice_text = " ".join(
            str(segments[index - 1].get("text", "") or "").strip()
            for index in segment_ids
        )
        normalised_expected = " ".join(
            re.findall(r"\w+", expected_voice_text.casefold(), flags=re.UNICODE)
        )
        normalised_scene_text = " ".join(
            re.findall(
                r"\w+",
                str(scene.get("voice_text", "") or "").casefold(),
                flags=re.UNICODE,
            )
        )
        if normalised_expected != normalised_scene_text:
            issues.append(
                PreflightIssue(
                    code="scene_voice_text_mismatch",
                    level="blocker",
                    scene_index=scene_id,
                    message=f"Текст сцены {scene_id} не совпадает с транскриптом.",
                    details={"segment_ids": segment_ids},
                )
            )
    missing_scene_segments = [
        index
        for index in range(1, len(segments) + 1)
        if index not in scene_covered_segments
    ]
    if missing_scene_segments:
        issues.append(
            PreflightIssue(
                code="uncovered_scene_segments",
                level="blocker",
                message="Часть транскрипта не входит ни в одну сцену.",
                details={"segment_ids": missing_scene_segments[:100]},
            )
        )

    shot_id_list = [int(shot.get("index", 0) or 0) for shot in shots]
    shot_ids = set(shot_id_list)
    duplicate_shot_ids = sorted(
        shot_id for shot_id in shot_ids if shot_id_list.count(shot_id) > 1
    )
    if duplicate_shot_ids:
        issues.append(
            PreflightIssue(
                code="duplicate_shot_ids",
                level="blocker",
                message="В плане повторяются идентификаторы кадров.",
                details={"shot_indices": duplicate_shot_ids},
            )
        )

    shots_by_scene: dict[int, list[dict[str, Any]]] = {}
    for shot in shots:
        scene_id = int(shot.get("scene_index", 0) or 0)
        shots_by_scene.setdefault(scene_id, []).append(shot)
        if scene_id not in scene_ids:
            issues.append(
                PreflightIssue(
                    code="shot_without_scene",
                    level="blocker",
                    scene_index=scene_id,
                    shot_index=int(shot.get("index", 0) or 0),
                    message="Кадр ссылается на отсутствующую сцену.",
                )
            )

    for scene in scenes:
        scene_id = int(scene.get("index", 0) or 0)
        scene_shots = sorted(
            shots_by_scene.get(scene_id, []),
            key=lambda shot: timecode_to_milliseconds(shot.get("start")),
        )
        if not scene_shots:
            issues.append(
                PreflightIssue(
                    code="scene_without_shots",
                    level="blocker",
                    scene_index=scene_id,
                    message=f"Сцена {scene_id} не имеет кадров.",
                )
            )
            continue
        scene_start = timecode_to_milliseconds(scene.get("start"))
        scene_end = timecode_to_milliseconds(scene.get("end"))
        if scene_end <= scene_start:
            issues.append(
                PreflightIssue(
                    code="invalid_scene_timing",
                    level="blocker",
                    scene_index=scene_id,
                    message=f"Сцена {scene_id} имеет нулевую или отрицательную длительность.",
                )
            )
            continue
        cursor = scene_start
        for shot in scene_shots:
            shot_id = int(shot.get("index", 0) or 0)
            shot_start = timecode_to_milliseconds(shot.get("start"))
            shot_end = timecode_to_milliseconds(shot.get("end"))
            if shot_end <= shot_start:
                issues.append(
                    PreflightIssue(
                        code="invalid_shot_timing",
                        level="blocker",
                        scene_index=scene_id,
                        shot_index=shot_id,
                        message=f"Кадр {shot_id} имеет нулевую или отрицательную длительность.",
                    )
                )
                continue
            delta = shot_start - cursor
            if delta > timing_tolerance_ms:
                issues.append(
                    PreflightIssue(
                        code="shot_timeline_gap",
                        level="blocker",
                        scene_index=scene_id,
                        shot_index=shot_id,
                        message=f"Перед кадром {shot_id} есть разрыв {delta / 1000:.2f} с.",
                    )
                )
            elif delta < -timing_tolerance_ms:
                issues.append(
                    PreflightIssue(
                        code="shot_timeline_overlap",
                        level="blocker",
                        scene_index=scene_id,
                        shot_index=shot_id,
                        message=f"Кадр {shot_id} накладывается на предыдущий на {-delta / 1000:.2f} с.",
                    )
                )
            cursor = max(cursor, shot_end)
        tail_delta = scene_end - cursor
        if tail_delta > timing_tolerance_ms:
            issues.append(
                PreflightIssue(
                    code="shot_timeline_gap",
                    level="blocker",
                    scene_index=scene_id,
                    shot_index=int(scene_shots[-1].get("index", 0) or 0),
                    message=f"В конце сцены {scene_id} есть разрыв {tail_delta / 1000:.2f} с.",
                )
            )
        elif tail_delta < -timing_tolerance_ms:
            issues.append(
                PreflightIssue(
                    code="shot_outside_scene",
                    level="blocker",
                    scene_index=scene_id,
                    shot_index=int(scene_shots[-1].get("index", 0) or 0),
                    message=f"Кадры выходят за границу сцены {scene_id} на {-tail_delta / 1000:.2f} с.",
                )
            )

    card_shot_id_list = [int(card.get("shot_index", 0) or 0) for card in cards]
    card_shot_ids = set(card_shot_id_list)
    card_ids = [str(card.get("card_id", "") or "").strip() for card in cards]
    if any(not card_id for card_id in card_ids):
        issues.append(
            PreflightIssue(
                code="missing_card_ids",
                level="blocker",
                message="Некоторые карточки материалов не имеют стабильного card_id.",
            )
        )
    duplicate_card_ids = sorted(
        {
            card_id
            for card_id in card_ids
            if card_id and card_ids.count(card_id) > 1
        }
    )
    if duplicate_card_ids:
        issues.append(
            PreflightIssue(
                code="duplicate_card_ids",
                level="blocker",
                message="В Asset Board повторяются стабильные card_id.",
                details={"card_ids": duplicate_card_ids[:50]},
            )
        )
    duplicate_card_shots = sorted(
        shot_id for shot_id in card_shot_ids if card_shot_id_list.count(shot_id) > 1
    )
    if duplicate_card_shots:
        issues.append(
            PreflightIssue(
                code="duplicate_asset_cards",
                level="blocker",
                message="Для одного кадра создано несколько карточек материалов.",
                details={"shot_indices": duplicate_card_shots},
            )
        )
    missing_cards = sorted(shot_ids - card_shot_ids)
    if missing_cards:
        issues.append(
            PreflightIssue(
                code="shots_without_cards",
                level="blocker",
                message="Asset Board не соответствует утверждённому шот-листу.",
                details={"shot_indices": missing_cards[:50]},
            )
        )
    extra_cards = sorted(card_shot_ids - shot_ids)
    if extra_cards:
        issues.append(
            PreflightIssue(
                code="cards_without_shots",
                level="blocker",
                message="Asset Board содержит карточки без соответствующих кадров.",
                details={"shot_indices": extra_cards[:50]},
            )
        )

    shots_by_id = {
        int(shot.get("index", 0) or 0): shot
        for shot in shots
    }
    for card in cards:
        shot_id = int(card.get("shot_index", 0) or 0)
        shot = shots_by_id.get(shot_id)
        if not shot:
            continue
        if (
            abs(
                timecode_to_milliseconds(card.get("start"))
                - timecode_to_milliseconds(shot.get("start"))
            )
            > timing_tolerance_ms
            or abs(
                timecode_to_milliseconds(card.get("end"))
                - timecode_to_milliseconds(shot.get("end"))
            )
            > timing_tolerance_ms
        ):
            issues.append(
                PreflightIssue(
                    code="card_timing_mismatch",
                    level="blocker",
                    scene_index=int(shot.get("scene_index", 0) or 0),
                    shot_index=shot_id,
                    message=f"Тайминг материала не совпадает с кадром {shot_id}.",
                )
            )
        mismatched_context = []
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
            shot_value = shot.get(key, [] if key == "subject_ids" else "")
            card_value = card.get(key, [] if key == "subject_ids" else "")
            if key == "subject_ids":
                matches = (
                    continuity.normalize_subject_ids(shot_value)
                    == continuity.normalize_subject_ids(card_value)
                )
            elif key == "continuity_mode":
                matches = (
                    continuity.normalize_continuity_mode(shot_value)
                    == continuity.normalize_continuity_mode(card_value)
                )
            else:
                matches = (
                    str(shot_value or "").strip()
                    == str(card_value or "").strip()
                )
            if not matches:
                mismatched_context.append(key)
        if mismatched_context:
            issues.append(
                PreflightIssue(
                    code="card_context_mismatch",
                    level="blocker",
                    scene_index=int(shot.get("scene_index", 0) or 0),
                    shot_index=shot_id,
                    message=(
                        f"Карточка материала {shot_id} не соответствует "
                        "утверждённому контексту кадра."
                    ),
                    details={"fields": mismatched_context},
                )
            )

    covered_segments: set[int] = set()
    for shot in shots:
        ids = overlapping_segment_ids(shot, segments)
        covered_segments.update(ids)
        if not ids:
            issues.append(
                PreflightIssue(
                    code="shot_without_voice_segment",
                    level="warning",
                    shot_index=int(shot.get("index", 0) or 0),
                    message="Кадр не связан ни с одним сегментом речи.",
                )
            )
    missing_segments = [
        index for index in range(1, len(segments) + 1) if index not in covered_segments
    ]
    if missing_segments:
        issues.append(
            PreflightIssue(
                code="uncovered_transcript_segments",
                level="blocker",
                message="Часть транскрипта не покрыта кадрами.",
                details={"segment_ids": missing_segments[:100]},
            )
        )

    # Each named fact belongs to the shot with the largest overlap with its
    # transcript segment. This avoids both silently dropping the fact and
    # incorrectly demanding that every shot in a scene repeat the same label.
    cards_by_shot: dict[int, list[dict[str, Any]]] = {}
    for card in cards:
        cards_by_shot.setdefault(int(card.get("shot_index", 0) or 0), []).append(card)
    anchor_requirements = _shot_anchor_requirements(shots, segments)
    for shot in shots:
        shot_id = int(shot.get("index", 0) or 0)
        scene_id = int(shot.get("scene_index", 0) or 0)
        anchors = anchor_requirements.get(shot_id, [])
        if not anchors:
            continue
        visual_text = " ".join(
            str(card.get(key, "") or "")
            for card in cards_by_shot.get(shot_id, [])
            for key in (
                "description",
                "asset_query",
                "screen_text",
                "text_overlay_text",
                "reference_query",
            )
        )
        missing = [anchor for anchor in anchors if not _contains_anchor(visual_text, anchor)]
        if missing:
            issues.append(
                PreflightIssue(
                    code="missing_visual_anchors",
                    level="warning",
                    scene_index=scene_id,
                    shot_index=shot_id,
                    message=(
                        f"Кадр {shot_id} сцены {scene_id} не закрепляет важные имена/числа: "
                        + ", ".join(missing[:8])
                    ),
                    details={"anchors": missing},
                )
            )

    # Catch the concrete failure seen in real projects: one concept occupying a
    # long consecutive run while narration has already moved on.
    previous_concept = ""
    run_seconds = 0.0
    run_shots: list[int] = []
    for card in cards:
        concept = _normalised_concept(card)
        duration = max(
            0.0,
            (
                timecode_to_milliseconds(card.get("end"))
                - timecode_to_milliseconds(card.get("start"))
            )
            / 1000.0,
        )
        if concept and concept == previous_concept:
            run_seconds += duration
            run_shots.append(int(card.get("shot_index", 0) or 0))
        else:
            if run_seconds > 12.0 and len(run_shots) > 1:
                issues.append(
                    PreflightIssue(
                        code="long_repeated_visual_concept",
                        level="warning",
                        shot_index=run_shots[0],
                        message=(
                            f"Один визуальный концепт занимает {run_seconds:.0f} секунд подряд."
                        ),
                        details={"shot_indices": run_shots},
                    )
                )
            previous_concept = concept
            run_seconds = duration
            run_shots = [int(card.get("shot_index", 0) or 0)]
    if run_seconds > 12.0 and len(run_shots) > 1:
        issues.append(
            PreflightIssue(
                code="long_repeated_visual_concept",
                level="warning",
                shot_index=run_shots[0],
                message=f"Один визуальный концепт занимает {run_seconds:.0f} секунд подряд.",
                details={"shot_indices": run_shots},
            )
        )

    canonical_plan = canonical_references.reference_plan(cards)
    if canonical_plan:
        # Static validation only: no prompt is queued and ComfyUI is not
        # contacted during preflight.
        from core.comfy_generation import reference_workflow_status
        from core.config import load_settings

        reference_status = reference_workflow_status(load_settings())
        if not reference_status.get("ok"):
            issues.append(
                PreflightIssue(
                    code="reference_workflow_unavailable",
                    level="blocker",
                    message=(
                        "Визуальный reference обязателен, но reference-workflow "
                        f"не готов: {reference_status.get('error', 'unknown error')}"
                    ),
                    details={
                        "mode": reference_status.get("mode"),
                        "groups": len(canonical_plan),
                    },
                )
            )
        elif not reference_status.get("enabled"):
            issues.append(
                PreflightIssue(
                    code="reference_conditioning_text_only",
                    level="warning",
                    message=(
                        "Связанные кадры используют Context Bible только текстом. "
                        "Для передачи одного эталона изображения всем кадрам "
                        "группы настройте marker-based reference-workflow."
                    ),
                    details={
                        "mode": reference_status.get("mode"),
                        "groups": len(canonical_plan),
                    },
                )
            )

    excluded = sum(
        str(card.get("status", "") or "").lower() == "skipped" for card in cards
    )
    ready = sum(card_is_ready(card) for card in cards)
    annotated_shots = annotate_shot_segments(shots, segments)
    plan_hash = (
        _plan_hash(scenes, annotated_shots, cards)
        if scenes or shots or cards
        else ""
    )
    return MaterialPreflight(
        allowed=not any(issue.level == "blocker" for issue in issues),
        plan_hash=plan_hash,
        scenes=len(scenes),
        shots=len(shots),
        cards=len(cards),
        segments=len(segments),
        pending_images=sum(card_needs_image(card) for card in cards),
        pending_videos=sum(card_needs_video(card) for card in cards),
        ready_cards=ready,
        excluded_cards=excluded,
        required_services=required_material_services(cards),
        issues=issues,
    )


def freeze_material_plan(
    project_dir: Path | str,
    preflight: MaterialPreflight,
    policy: ProductionPolicy,
) -> dict[str, Any]:
    root = Path(project_dir)
    segments = _read_json(root / "01_transcript_segments.json", [])
    shots = annotate_shot_segments(
        _read_json(root / "04_shot_list.json", []),
        segments,
    )
    cards = _read_json(root / "06_asset_board.json", [])
    scenes = _read_json(root / "02_scenes.json", [])
    shot_by_index = {
        int(shot.get("index", 0) or 0): shot
        for shot in shots
    }
    anchor_requirements = _shot_anchor_requirements(shots, segments)
    context_bible = continuity.load_context_bible(root)
    from core.comfy_generation import reference_workflow_status
    from core.config import load_settings

    reference_status = reference_workflow_status(load_settings())
    visual_concepts: list[VisualConcept] = []
    for shot in shots:
        scene_index = int(shot.get("scene_index", 0) or 0)
        shot_index = int(shot.get("index", 0) or 0)
        visual_concepts.append(
            VisualConcept(
                id=str(shot["visual_concept_id"]),
                scene_index=scene_index,
                segment_ids=list(shot.get("segment_ids", [])),
                description=str(shot.get("description", "") or ""),
                required_visual_anchors=anchor_requirements.get(shot_index, []),
                purpose=str(shot.get("purpose", "") or ""),
                continuity_mode=continuity.normalize_continuity_mode(
                    shot.get("continuity_mode")
                ),
                continuity_group_id=str(
                    shot.get("continuity_group_id", "") or ""
                ),
                location_id=str(shot.get("location_id", "") or ""),
                subject_ids=continuity.normalize_subject_ids(
                    shot.get("subject_ids")
                ),
                state_before=str(shot.get("state_before", "") or ""),
                action=str(shot.get("action", "") or ""),
                state_after=str(shot.get("state_after", "") or ""),
            )
        )
    asset_requests: list[AssetRequest] = []
    edit_spans: list[EditSpan] = []
    for card in cards:
        shot_index = int(card.get("shot_index", 0) or 0)
        shot = shot_by_index.get(shot_index, {})
        concept_id = str(
            shot.get("visual_concept_id")
            or f"scene-{int(card.get('scene_index', 0) or 0):03d}-"
            f"shot-{shot_index:03d}"
        )
        request_id = f"asset-{str(card.get('card_id', '') or shot_index)}"
        asset_requests.append(
            AssetRequest(
                id=request_id,
                visual_concept_id=concept_id,
                route=str(card.get("route", "") or card.get("visual_type", "")),
                image_prompt=str(card.get("image_prompt", "") or ""),
                video_prompt=str(card.get("video_prompt", "") or ""),
                compiled_image_prompt=continuity.compile_generation_prompt(
                    str(card.get("image_prompt", "") or ""),
                    card,
                    target="image",
                    context_bible=context_bible,
                ),
                compiled_video_prompt=continuity.compile_generation_prompt(
                    str(
                        card.get("video_prompt", "")
                        or card.get("image_prompt", "")
                        or ""
                    ),
                    card,
                    target="video",
                    context_bible=context_bible,
                ),
                motion=str(card.get("motion", "") or ""),
                reference_frame_id=str(
                    card.get("reference_frame_id", "") or ""
                ),
            )
        )
        edit_spans.append(
            EditSpan(
                id=f"span-{shot_index:03d}",
                scene_index=int(card.get("scene_index", 0) or 0),
                shot_index=shot_index,
                visual_concept_id=concept_id,
                asset_request_id=request_id,
                start_ms=timecode_to_milliseconds(card.get("start")),
                end_ms=timecode_to_milliseconds(card.get("end")),
            )
        )
    snapshot = {
        "schema_version": MATERIAL_PLAN_SCHEMA_VERSION,
        "created_at": utc_now(),
        "plan_hash": preflight.plan_hash,
        "policy": asdict(policy),
        "scenes": scenes,
        "shots": shots,
        "context_bible": context_bible,
        "reference_plan": canonical_references.reference_plan(cards),
        "reference_conditioning": {
            key: reference_status.get(key)
            for key in ("mode", "enabled", "workflow_sha256")
            if reference_status.get(key) not in (None, "")
        },
        "visual_concepts": [
            {
                key: value
                for key, value in asdict(item).items()
                if key
                not in {
                    "purpose",
                    "continuity_mode",
                    "continuity_group_id",
                    "location_id",
                    "subject_ids",
                    "state_before",
                    "action",
                    "state_after",
                }
                or value not in ("", [], None, "independent")
            }
            for item in visual_concepts
        ],
        "asset_requests": [
            {
                key: value
                for key, value in asdict(item).items()
                if key != "reference_frame_id" or value
            }
            for item in asset_requests
        ],
        "edit_spans": [asdict(item) for item in edit_spans],
        "operations": build_material_operations(
            cards,
            skip_ready=policy.skip_ready,
        ),
        "cards": [
            {
                key: card.get(key)
                for key in MATERIAL_PLAN_CARD_FIELDS
            }
            for card in cards
        ],
    }
    snapshot["snapshot_hash"] = content_hash(snapshot)
    atomic_write_text(
        root / MATERIAL_PLAN_FILE,
        json.dumps(snapshot, ensure_ascii=False, indent=2),
    )
    return snapshot


def load_material_plan(project_dir: Path | str) -> dict[str, Any] | None:
    """Load a frozen plan only when its content still matches its fingerprint."""

    data = _read_json(Path(project_dir) / MATERIAL_PLAN_FILE, None)
    if not isinstance(data, dict):
        return None
    schema_version = int(data.get("schema_version", 0) or 0)
    if schema_version not in MATERIAL_PLAN_SUPPORTED_SCHEMA_VERSIONS:
        return None
    scenes = data.get("scenes")
    shots = data.get("shots")
    cards = data.get("cards")
    operations = data.get("operations")
    if not all(
        isinstance(items, list) for items in (scenes, shots, cards, operations)
    ):
        return None
    expected_hash = str(data.get("plan_hash", "") or "")
    hash_profiles = (
        (MATERIAL_PLAN_V1_HASH_CARD_FIELDS, MATERIAL_PLAN_HASH_CARD_FIELDS)
        if schema_version == 1
        else (MATERIAL_PLAN_HASH_CARD_FIELDS,)
    )
    if not expected_hash or not any(
        _plan_hash(scenes, shots, cards, card_fields=fields) == expected_hash
        for fields in hash_profiles
    ):
        return None
    snapshot_hash = str(data.get("snapshot_hash", "") or "")
    integrity_payload = {
        key: value for key, value in data.items() if key != "snapshot_hash"
    }
    if not snapshot_hash or content_hash(integrity_payload) != snapshot_hash:
        return None
    return data


def material_plan_matches_project(
    project_dir: Path | str,
    plan: dict[str, Any],
) -> bool:
    """Compare a frozen plan with current creative inputs using its own schema."""

    root = Path(project_dir)
    segments = _read_json(root / "01_transcript_segments.json", [])
    scenes = _read_json(root / "02_scenes.json", [])
    shots = annotate_shot_segments(
        _read_json(root / "04_shot_list.json", []),
        segments if isinstance(segments, list) else [],
    )
    cards = _read_json(root / "06_asset_board.json", [])
    if not all(isinstance(items, list) for items in (scenes, shots, cards)):
        return False
    schema_version = int(plan.get("schema_version", 0) or 0)
    frozen_scenes = plan.get("scenes", [])
    frozen_shots = plan.get("shots", [])
    frozen_cards = plan.get("cards", [])
    expected_hash = str(plan.get("plan_hash", "") or "")
    profiles = (
        (MATERIAL_PLAN_V1_HASH_CARD_FIELDS, MATERIAL_PLAN_HASH_CARD_FIELDS)
        if schema_version == 1
        else (MATERIAL_PLAN_HASH_CARD_FIELDS,)
    )
    for fields in profiles:
        frozen_hash = _plan_hash(
            frozen_scenes,
            frozen_shots,
            frozen_cards,
            card_fields=fields,
        )
        if frozen_hash != expected_hash:
            continue
        return (
            _plan_hash(scenes, shots, cards, card_fields=fields)
            == expected_hash
        )
    return False


def new_material_run(
    project_dir: Path | str,
    project_id: str,
    preflight: MaterialPreflight,
    policy: ProductionPolicy,
) -> dict[str, Any]:
    now = utc_now()
    run = {
        "schema_version": MATERIAL_RUN_SCHEMA_VERSION,
        "run_id": uuid.uuid4().hex,
        "project_id": str(project_id),
        "plan_hash": preflight.plan_hash,
        "status": "queued",
        "policy": asdict(policy),
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "status_history": [{"status": "queued", "at": now}],
        "current": {},
        "cards": {},
        "summary": {
            "cards_total": preflight.cards,
            "cards_ready": preflight.ready_cards,
            "operations_total": preflight.pending_images + preflight.pending_videos,
            "operations_completed": 0,
            "retries": 0,
            "fallbacks": 0,
            "failures": 0,
        },
        "error": "",
    }
    save_material_run(project_dir, run)
    return run


def transition_material_run(
    run: dict[str, Any],
    status: MaterialRunStatus,
    *,
    reason: str = "",
) -> None:
    """Apply one explicit state-machine transition and retain a compact audit."""

    current = str(run.get("status", "") or "")
    if current == status:
        return
    if status not in _MATERIAL_RUN_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid material run transition: {current} -> {status}")
    run["status"] = status
    history = run.setdefault("status_history", [])
    history.append(
        {
            "status": status,
            "at": utc_now(),
            **({"reason": reason} if reason else {}),
        }
    )
    if len(history) > 200:
        del history[:-200]


def _material_run_is_valid(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if int(data.get("schema_version", 0) or 0) != MATERIAL_RUN_SCHEMA_VERSION:
        return False
    checksum = str(data.get("checkpoint_hash", "") or "")
    if not checksum:
        # Backward-compatible one-time upgrade for checkpoints created before
        # integrity hashes existed.
        return True
    payload = {key: value for key, value in data.items() if key != "checkpoint_hash"}
    return content_hash(payload) == checksum


def load_material_run(project_dir: Path | str) -> dict[str, Any] | None:
    root = Path(project_dir)
    primary = _read_json(root / MATERIAL_RUN_FILE, None)
    if _material_run_is_valid(primary):
        return primary
    backup = _read_json(root / MATERIAL_RUN_BACKUP_FILE, None)
    return backup if _material_run_is_valid(backup) else None


def save_material_run(project_dir: Path | str, run: dict[str, Any]) -> None:
    root = Path(project_dir)
    run["updated_at"] = utc_now()
    current = _read_json(root / MATERIAL_RUN_FILE, None)
    if _material_run_is_valid(current):
        atomic_write_text(
            root / MATERIAL_RUN_BACKUP_FILE,
            json.dumps(current, ensure_ascii=False, indent=2),
        )
    run.pop("checkpoint_hash", None)
    run["checkpoint_hash"] = content_hash(run)
    atomic_write_text(
        root / MATERIAL_RUN_FILE,
        json.dumps(run, ensure_ascii=False, indent=2),
    )


__all__ = [
    "MATERIAL_PLAN_FILE",
    "MATERIAL_RUN_BACKUP_FILE",
    "MATERIAL_RUN_FILE",
    "AssetRequest",
    "EditSpan",
    "MaterialPreflight",
    "PreflightIssue",
    "ProductionPolicy",
    "VisualConcept",
    "annotate_shot_segments",
    "build_material_operations",
    "card_is_ready",
    "card_needs_image",
    "card_needs_video",
    "freeze_material_plan",
    "load_material_plan",
    "load_material_run",
    "material_plan_matches_project",
    "new_material_run",
    "overlapping_segment_ids",
    "preflight_material_plan",
    "required_material_services",
    "save_material_run",
    "timecode_to_milliseconds",
    "transition_material_run",
    "utc_now",
]
