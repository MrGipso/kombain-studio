"""Structured context and continuity contracts for storyboard planning.

The continuity bible is compiled before material generation. It contains no
provider-specific state: ComfyUI, stock and future renderers all consume the
same scene/shot identities and state transitions.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from core.models import Scene, Shot
from core.project import atomic_write_text, content_hash


CONTEXT_BIBLE_FILE = "03_context_bible.json"
CONTEXT_BIBLE_SCHEMA_VERSION = 1
CONTINUITY_MODES = {
    "independent",
    "same_location",
    "continuous_action",
    "recurring_subject",
    "evidence_sequence",
}

_MODE_ALIASES = {
    "broll": "independent",
    "b_roll": "independent",
    "standalone": "independent",
    "same place": "same_location",
    "same_location_sequence": "same_location",
    "continuous": "continuous_action",
    "action": "continuous_action",
    "character": "recurring_subject",
    "recurring_character": "recurring_subject",
    "evidence": "evidence_sequence",
    "documentary_evidence": "evidence_sequence",
}


def stable_context_id(value: Any, prefix: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    text = re.sub(r"[^a-zа-яё0-9]+", "-", text, flags=re.IGNORECASE).strip("-")
    if not text:
        return f"{prefix}-unknown"
    return f"{prefix}-{text[:64]}"


def normalize_continuity_mode(value: Any) -> str:
    mode = re.sub(r"[\s-]+", "_", str(value or "independent").strip().casefold())
    mode = _MODE_ALIASES.get(mode, mode)
    return mode if mode in CONTINUITY_MODES else "independent"


def normalize_subject_ids(value: Any, fallback: str = "") -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,;|]+", str(value or ""))
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        subject_id = (
            text
            if re.fullmatch(r"subject-[a-zа-яё0-9-]+", text, flags=re.IGNORECASE)
            else stable_context_id(text, "subject")
        )
        if subject_id not in result:
            result.append(subject_id)
    if not result and fallback:
        result.append(stable_context_id(fallback, "subject"))
    return result


def _plan_map(global_plan: Iterable[dict[str, Any] | Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in global_plan:
        raw = item if isinstance(item, dict) else asdict(item)
        scene_index = int(raw.get("scene_index", 0) or 0)
        if scene_index:
            result[scene_index] = dict(raw)
    return result


def apply_storyboard_context(
    shots: list[Shot],
    global_plan: Iterable[dict[str, Any] | Any],
) -> list[Shot]:
    """Fill missing continuity fields deterministically after LM planning."""

    plan_by_scene = _plan_map(global_plan)
    by_scene: dict[int, list[Shot]] = {}
    for shot in shots:
        by_scene.setdefault(shot.scene_index, []).append(shot)

    previous_global: Shot | None = None
    group_directions: dict[str, str] = {}
    for scene_index in sorted(by_scene):
        scene_shots = sorted(by_scene[scene_index], key=lambda shot: shot.index)
        strategy = plan_by_scene.get(scene_index, {})
        mode = normalize_continuity_mode(strategy.get("continuity_mode"))
        location_id = str(strategy.get("location_id", "") or "").strip()
        if not location_id:
            location_id = stable_context_id(strategy.get("location", ""), "location")
        else:
            location_id = re.sub(
                r"[^a-zа-яё0-9_-]+",
                "-",
                location_id.casefold(),
                flags=re.IGNORECASE,
            ).strip("-_") or stable_context_id(
                strategy.get("location", ""),
                "location",
            )
        subjects = normalize_subject_ids(
            strategy.get("subject_ids"),
            str(strategy.get("primary_subject", "") or ""),
        )
        entry_state = str(strategy.get("entry_state", "") or "").strip()
        exit_state = str(strategy.get("exit_state", "") or "").strip()
        scene_event = str(strategy.get("scene_event", "") or "").strip()
        story_thread_id = stable_context_id(
            strategy.get("story_thread", f"scene-{scene_index}"),
            "thread",
        )
        continuous_run = 0
        previous_mode = ""

        for local_index, shot in enumerate(scene_shots):
            previous = scene_shots[local_index - 1] if local_index else previous_global
            authored_mode = normalize_continuity_mode(shot.continuity_mode)
            shot.continuity_mode = (
                mode if mode != "independent" else authored_mode
            )
            shot.location_id = (
                location_id
                if shot.continuity_mode != "independent"
                else shot.location_id.strip() or location_id
            )
            if shot.continuity_mode == "recurring_subject" and subjects:
                # The group follows one canonical recurring identity. Secondary
                # props may change from scene to scene and must not redefine it.
                shot.subject_ids = [subjects[0]]
            elif shot.continuity_mode != "independent" and subjects:
                shot.subject_ids = list(subjects)
            else:
                shot.subject_ids = (
                    normalize_subject_ids(shot.subject_ids) or list(subjects)
                )
            # Group identity is an engine contract, not creative prose.  Local
            # models frequently reuse a plausible label ("ship interior") in
            # unrelated scenes or mix modes inside one authored label.  Trusting
            # it creates cross-film state chains and blocks unattended material
            # generation after the expensive storyboard pass has completed.
            if shot.continuity_mode == "independent":
                shot.continuity_group_id = f"shot-{shot.index:03d}"
            elif shot.continuity_mode == "recurring_subject" and shot.subject_ids:
                shot.continuity_group_id = (
                    f"group-recurring-{shot.subject_ids[0]}"
                )
            elif shot.continuity_mode == "same_location":
                shot.continuity_group_id = f"group-location-{location_id}"
            elif shot.continuity_mode == "evidence_sequence":
                shot.continuity_group_id = f"group-evidence-{story_thread_id}"
            else:
                # Continuous action is scene-bounded unless a future authored
                # contract explicitly models a cross-scene causal hand-off.
                # A standalone/evidence insert also breaks the action run; two
                # action fragments around that insert are not one causal chain.
                if previous_mode != "continuous_action":
                    continuous_run += 1
                shot.continuity_group_id = (
                    f"group-scene-{scene_index:03d}-action-"
                    f"{continuous_run:02d}-{location_id}"
                )
            if not shot.purpose.strip():
                if len(scene_shots) == 1:
                    shot.purpose = "Раскрыть смысл сцены"
                elif local_index == 0:
                    shot.purpose = "Установить место, субъект и исходное состояние"
                elif local_index == len(scene_shots) - 1:
                    shot.purpose = "Показать результат и подготовить переход"
                else:
                    shot.purpose = "Развить действие или показать важную деталь"
            previous_state = ""
            if previous is not None and (
                local_index > 0
                or previous.continuity_group_id == shot.continuity_group_id
            ):
                previous_state = previous.state_after.strip()
            same_continuous_action = (
                previous is not None
                and shot.continuity_mode == "continuous_action"
                and previous.continuity_group_id == shot.continuity_group_id
                and bool(previous_state)
            )
            if same_continuous_action:
                # A continuous action has one canonical causal boundary. An LM
                # may paraphrase the next input state, but downstream engines
                # need an exact hand-off that can be validated without visual QA.
                shot.state_before = previous_state
            else:
                shot.state_before = (
                    shot.state_before.strip()
                    or previous_state
                    or entry_state
                    or "Исходное состояние сцены"
                )
            shot.action = shot.action.strip() or shot.description.strip() or scene_event
            shot.state_after = (
                shot.state_after.strip()
                or (
                    exit_state
                    if local_index == len(scene_shots) - 1
                    else f"После события кадра {shot.index}: {shot.action[:180]}"
                )
            )
            shot.shot_size = shot.shot_size.strip() or _infer_shot_size(shot)
            shot.camera_angle = shot.camera_angle.strip() or "eye_level"
            direction = re.sub(
                r"[\s-]+",
                "_",
                shot.screen_direction.strip().casefold(),
            )
            if direction not in {"left_to_right", "right_to_left", "neutral"}:
                direction = "neutral"
            if shot.continuity_mode != "independent" and direction != "neutral":
                canonical_direction = group_directions.setdefault(
                    shot.continuity_group_id,
                    direction,
                )
                direction = canonical_direction
            shot.screen_direction = direction
            if shot.continuity_mode in {
                "same_location",
                "continuous_action",
                "recurring_subject",
            }:
                reference_key = (
                    shot.subject_ids[0]
                    if shot.continuity_mode == "recurring_subject" and shot.subject_ids
                    else shot.location_id
                )
                shot.reference_frame_id = stable_context_id(
                    reference_key,
                    "reference",
                )
            shot.transition_in = (
                shot.transition_in.strip()
                or ("Начало сцены" if local_index == 0 else "Стык по действию или смыслу")
            )
            shot.transition_out = (
                shot.transition_out.strip()
                or (
                    str(strategy.get("transition", "") or "").strip()
                    if local_index == len(scene_shots) - 1
                    else "Продолжение сцены"
                )
            )
            if not shot.change_from_previous.strip():
                shot.change_from_previous = (
                    "Первый кадр проекта"
                    if previous is None
                    else f"Новая крупность {shot.shot_size}; действие продвигается"
                )
            previous_mode = shot.continuity_mode
        previous_global = scene_shots[-1]
    return shots


def _infer_shot_size(shot: Shot) -> str:
    text = f"{shot.image_prompt} {shot.description}".casefold()
    if any(marker in text for marker in ("extreme close", "macro", "макро")):
        return "detail"
    if any(marker in text for marker in ("close-up", "close up", "крупн")):
        return "close"
    if any(marker in text for marker in ("wide", "aerial", "общий")):
        return "wide"
    return "medium"


def build_context_bible(
    scenes: list[Scene] | list[dict[str, Any]],
    global_plan: Iterable[dict[str, Any] | Any],
    shots: list[Shot] | list[dict[str, Any]],
    *,
    character_description: str = "",
) -> dict[str, Any]:
    plan_source = [
        dict(item) if isinstance(item, dict) else asdict(item)
        for item in global_plan
    ]
    plan_by_scene = _plan_map(plan_source)
    scene_rows: list[dict[str, Any]] = []
    entities: dict[str, dict[str, Any]] = {}
    locations: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[str, Any]] = {}

    for raw_scene in scenes:
        scene = raw_scene if isinstance(raw_scene, dict) else asdict(raw_scene)
        scene_index = int(scene.get("index", 0) or 0)
        strategy = plan_by_scene.get(scene_index, {})
        location_id = str(strategy.get("location_id", "") or "").strip()
        if not location_id:
            location_id = stable_context_id(strategy.get("location", ""), "location")
        subject_ids = normalize_subject_ids(
            strategy.get("subject_ids"),
            str(strategy.get("primary_subject", "") or ""),
        )
        mode = normalize_continuity_mode(strategy.get("continuity_mode"))
        scene_rows.append(
            {
                "scene_index": scene_index,
                "story_thread": str(strategy.get("story_thread", "") or scene.get("title", "")),
                "continuity_mode": mode,
                "location_id": location_id,
                "subject_ids": subject_ids,
                "entry_state": str(strategy.get("entry_state", "") or ""),
                "scene_event": str(strategy.get("scene_event", "") or scene.get("visual_direction", "")),
                "exit_state": str(strategy.get("exit_state", "") or ""),
                "transition_out": str(strategy.get("transition", "") or ""),
            }
        )
        locations.setdefault(
            location_id,
            {
                "id": location_id,
                "description": str(strategy.get("location", "") or ""),
                "scene_indices": [],
            },
        )["scene_indices"].append(scene_index)
        for subject_id in subject_ids:
            entity = entities.setdefault(
                subject_id,
                {
                    "id": subject_id,
                    "description": str(strategy.get("primary_subject", "") or ""),
                    "appearance_anchor": character_description,
                    "scene_indices": [],
                },
            )
            entity["scene_indices"].append(scene_index)

    shot_rows: list[dict[str, Any]] = []
    for raw_shot in shots:
        shot = raw_shot if isinstance(raw_shot, dict) else asdict(raw_shot)
        row = {
            key: shot.get(key)
            for key in (
                "index",
                "scene_index",
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
        }
        shot_rows.append(row)
        group_id = str(row.get("continuity_group_id", "") or "")
        if group_id:
            group = groups.setdefault(
                group_id,
                {
                    "id": group_id,
                    "mode": normalize_continuity_mode(row.get("continuity_mode")),
                    "location_id": str(row.get("location_id", "") or ""),
                    "subject_ids": normalize_subject_ids(row.get("subject_ids")),
                    "shot_indices": [],
                },
            )
            group["shot_indices"].append(int(row.get("index", 0) or 0))

    scene_source = [
        dict(item) if isinstance(item, dict) else asdict(item)
        for item in scenes
    ]
    shot_source = [
        dict(item) if isinstance(item, dict) else asdict(item)
        for item in shots
    ]
    bible = {
        "schema_version": CONTEXT_BIBLE_SCHEMA_VERSION,
        "source_hash": context_source_hash(scene_source, plan_source, shot_source),
        "scenes": scene_rows,
        "entities": list(entities.values()),
        "locations": list(locations.values()),
        "continuity_groups": list(groups.values()),
        "shots": shot_rows,
    }
    bible["context_hash"] = content_hash(bible)
    return bible


def context_source_hash(
    scenes: list[dict[str, Any]],
    global_plan: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> str:
    context_fields = (
        "index",
        "scene_index",
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
    compact_shots = [
        {key: shot.get(key) for key in context_fields}
        for shot in shots
    ]
    return content_hash(
        {
            "scenes": scenes,
            "global_plan": global_plan,
            "shots": compact_shots,
        }
    )


def write_context_bible(project_dir: Path | str, bible: dict[str, Any]) -> Path:
    path = Path(project_dir) / CONTEXT_BIBLE_FILE
    atomic_write_text(path, json.dumps(bible, ensure_ascii=False, indent=2))
    return path


def load_context_bible(project_dir: Path | str) -> dict[str, Any] | None:
    path = Path(project_dir) / CONTEXT_BIBLE_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("schema_version", 0) or 0) != CONTEXT_BIBLE_SCHEMA_VERSION:
        return None
    expected = str(data.get("context_hash", "") or "")
    payload = {key: value for key, value in data.items() if key != "context_hash"}
    if not expected or content_hash(payload) != expected:
        return None
    return data


def _context_value(item: dict[str, Any] | Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _prompt_value(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip()


def compile_generation_prompt(
    base_prompt: str,
    item: dict[str, Any] | Any,
    *,
    target: str,
    context_bible: dict[str, Any] | None = None,
) -> str:
    """Compile frozen storyboard context into a provider-facing prompt.

    This is intentionally textual conditioning. Canonical image references are
    represented by ``reference_frame_id`` elsewhere and require a workflow that
    exposes a real reference-image input; this compiler never pretends that an
    opaque ID alone can preserve identity.
    """

    prompt = str(base_prompt or "").strip()
    if target not in {"image", "video"}:
        raise ValueError("target must be 'image' or 'video'")
    mode = normalize_continuity_mode(_context_value(item, "continuity_mode"))
    purpose = _prompt_value(_context_value(item, "purpose"))
    action = _prompt_value(_context_value(item, "action"))
    state_before = _prompt_value(_context_value(item, "state_before"))
    state_after = _prompt_value(_context_value(item, "state_after"))
    location_id = _prompt_value(_context_value(item, "location_id"), 100)
    subject_ids = normalize_subject_ids(_context_value(item, "subject_ids"))
    shot_size = _prompt_value(_context_value(item, "shot_size"), 40)
    camera_angle = _prompt_value(_context_value(item, "camera_angle"), 40)
    screen_direction = _prompt_value(
        _context_value(item, "screen_direction"),
        40,
    )
    context_aware = any(
        (
            purpose,
            action,
            state_before,
            state_after,
            location_id,
            subject_ids,
            shot_size,
            camera_angle,
            screen_direction,
        )
    )
    if not context_aware:
        return prompt

    locations: dict[str, dict[str, Any]] = {}
    entities: dict[str, dict[str, Any]] = {}
    if isinstance(context_bible, dict):
        locations = {
            str(row.get("id", "")): row
            for row in context_bible.get("locations", [])
            if isinstance(row, dict) and row.get("id")
        }
        entities = {
            str(row.get("id", "")): row
            for row in context_bible.get("entities", [])
            if isinstance(row, dict) and row.get("id")
        }

    location = _prompt_value(
        locations.get(location_id, {}).get("description", ""),
        260,
    )
    subject_descriptions: list[str] = []
    for subject_id in subject_ids:
        entity = entities.get(subject_id, {})
        description = _prompt_value(entity.get("description", ""), 180)
        appearance = _prompt_value(entity.get("appearance_anchor", ""), 220)
        combined = "; ".join(
            part for part in (description, appearance) if part
        )
        if combined and combined not in subject_descriptions:
            subject_descriptions.append(combined)

    constraints: list[str] = [f"continuity mode: {mode}"]
    if purpose:
        constraints.append(f"editorial purpose: {purpose}")
    if location:
        constraints.append(f"keep this location: {location}")
    if subject_descriptions:
        constraints.append(
            "keep these subjects: " + " | ".join(subject_descriptions)
        )
    if target == "image":
        if action:
            constraints.append(f"captured action: {action}")
        if state_after:
            constraints.append(f"visible resulting state: {state_after}")
    else:
        if state_before:
            constraints.append(f"initial visible state: {state_before}")
        if action:
            constraints.append(f"single continuous action: {action}")
        if state_after:
            constraints.append(f"final visible state: {state_after}")
        constraints.append(
            "preserve the identity, wardrobe, objects and location of the input frame"
        )
    composition = ", ".join(
        value
        for value in (
            f"shot size {shot_size}" if shot_size else "",
            f"camera angle {camera_angle}" if camera_angle else "",
            (
                f"screen direction {screen_direction}"
                if screen_direction and screen_direction != "neutral"
                else ""
            ),
        )
        if value
    )
    if composition:
        constraints.append(f"composition: {composition}")

    block = "Storyboard continuity constraints: " + ". ".join(constraints) + "."
    return f"{prompt}\n{block}" if prompt else block


__all__ = [
    "CONTEXT_BIBLE_FILE",
    "CONTEXT_BIBLE_SCHEMA_VERSION",
    "CONTINUITY_MODES",
    "apply_storyboard_context",
    "build_context_bible",
    "compile_generation_prompt",
    "context_source_hash",
    "load_context_bible",
    "normalize_continuity_mode",
    "normalize_subject_ids",
    "stable_context_id",
    "write_context_bible",
]
