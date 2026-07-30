"""Canonical, UI-independent Asset Board operations.

The board is the editable source of truth shared by the desktop and web
frontends.  Keeping its persistence and route-normalisation rules in ``core``
prevents backend workers from importing the Tk application merely to read or
write ``06_asset_board.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict
from pathlib import Path

from core import asset_variants
from core.models import (
    AssetCard,
    Scene,
    Shot,
    legacy_asset_card_id,
    normalize_numeric_screen_text,
)
from core.project import atomic_write_text, open_project


ASSET_BOARD_FILE = "06_asset_board.json"

_ABSTRACT_MARKERS = (
    "символизир",
    "олицетвор",
    "метафор",
    "как символ",
    "в виде символа",
    "представляет собой",
    "это образ",
    "abstract concept",
    "as a metaphor",
)

_OVERLAY_STOPWORDS = {
    "и", "а", "но", "что", "чтобы", "это", "этот", "эта", "вот", "потому",
    "поэтому", "если", "когда", "как", "так", "то", "же", "ведь", "значит",
    "вы", "мы", "он", "она", "они", "оно", "я", "ты", "не", "ни", "или",
    "хотя", "просто", "теперь", "затем", "потом", "давайте", "начнём",
    "and", "but", "so", "because", "if", "when", "you", "we", "it", "this",
    "that", "the", "now", "then", "just",
}

_LEGACY_FIELD_DEFAULTS: dict[str, object] = {
    "image_output": "",
    "video_output": "",
    "remotion_template": "",
    "remotion_output": "",
    "status": "planned",
    "notes": "",
    "approved": False,
    "timeline_start": "",
    "timeline_end": "",
    "assigned_media": "",
    "track": "video_1",
    "text_overlay_enabled": False,
    "text_overlay_text": "",
    "text_overlay_position": "bottom",
    "text_overlay_style": "subtitle",
    "text_overlay_x": 50.0,
    "text_overlay_y": 86.0,
    "text_overlay_enter": "fade",
    "text_overlay_exit": "fade",
    "plan_fingerprint": "",
}

_MEDIA_PLAN_FIELDS = (
    "scene_index",
    "start",
    "end",
    "visual_type",
    "description",
    "asset_query",
    "image_model",
    "video_model",
    "image_prompt",
    "video_prompt",
    "motion",
    "fallback",
    "reference_query",
    "reference_source",
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


def media_plan_fingerprint(item: Shot | AssetCard) -> str:
    """Return a stable identity for the media a shot is asking us to create.

    ``shot_index`` is intentionally excluded: it is presentation order, not
    provenance.  Overlay and SFX fields are also excluded because changing
    either must not force an expensive image/video regeneration.
    """

    payload = {
        field: str(getattr(item, field, "") or "").strip()
        for field in _MEDIA_PLAN_FIELDS
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _card_matches_shot_plan(card: AssetCard, shot: Shot) -> bool:
    """True only when preserving the card's generated media is safe."""

    current = media_plan_fingerprint(shot)
    recorded = (card.plan_fingerprint or "").strip()
    if recorded:
        return recorded == current
    # Legacy boards did not store a fingerprint.  Derive it from their full
    # media plan so an unchanged card survives the migration, while hybrid
    # rows containing a new description and old prompts are invalidated.
    return media_plan_fingerprint(card) == current


def preferred_realistic_image_model(image_preference: str) -> str:
    return "ideogram4" if (image_preference or "").strip().lower() == "ideogram" else "krea2_turbo"


def _normalize_shot_routes(shots: list[Shot], image_preference: str = "Auto") -> None:
    preferred_model = preferred_realistic_image_model(image_preference)
    preference = (image_preference or "").strip().lower()
    for shot in shots:
        visual_type = (shot.visual_type or "").lower()
        if visual_type == "stock_video":
            continue
        if visual_type == "still_kenburns":
            if shot.image_model != "ideogram4" or preference in {"krea", "ideogram"}:
                shot.image_model = preferred_model
            shot.video_model = ""
            continue
        if visual_type == "ai_typography_card":
            continue
        if visual_type == "web_reference_card":
            shot.image_model = ""
            shot.video_model = ""
            continue
        if shot.visual_type not in {"ai_image", "ai_image_then_i2v"}:
            shot.visual_type = "ai_image_then_i2v"
        if shot.image_model != "ideogram4" or preference in {"krea", "ideogram"}:
            shot.image_model = preferred_model
        shot.video_model = shot.video_model or "ltx2.3_i2v"
        if not shot.fallback or "Remotion" in shot.fallback or "SVG" in shot.fallback:
            shot.fallback = "AI image-to-video generation"
            if shot.visual_type == "ai_image":
                shot.visual_type = "ai_image_then_i2v"


def _suggest_shot_type(text: str) -> str:
    lower = text.lower()
    rules = (
        (("macro", "detail", "волокн", "cable", "close", "microscop"), "extreme macro detail shot"),
        (("aerial", "вид сверху", "overview", "data center", "city", "город"), "wide aerial establishing shot"),
        (("портрет", "face", "portrait", "person", "человек", "инженер", "учён"), "medium close-up portrait with environmental context"),
        (("underwater", "океан", "под водой", "ocean floor", "море", "дно"), "underwater wide shot"),
        (("ship", "судно", "кораб", "vessel", "лодк"), "dramatic low angle wide shot"),
        (("building", "здани", "architecture", "архитектур", "factory", "завод"), "low angle architectural shot"),
        (("server", "сервер", "data center", "datacenter"), "symmetrical wide shot of server rows"),
        (("hand", "рука", "палец", "fingers", "инструмент", "tool"), "extreme close-up of hands at work"),
        (("историческ", "archive", "архив", "старин", "old", "antique", "винтаж"), "vintage archival still"),
        (("terminal", "screen", "бирж", "stock"), "medium shot of glowing screens"),
    )
    for keywords, shot_type in rules:
        if any(word in lower for word in keywords):
            return shot_type
    return "medium establishing shot"


def _clean_visual_prompt_source(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip().replace("->", ", then ")
    cleaned = re.sub(
        r"\b(?:Supporting B-roll or graphic for scene|Closing visual for scene|Scene idea):\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" .,:;") or "documentary technology visual"


def _prompt_has_too_much_transcript(prompt: str) -> bool:
    if not prompt.strip():
        return True
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", prompt))
    letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", prompt))
    if letters and cyrillic / letters > 0.18:
        return True
    markers = (
        "вот вы отправили",
        "если спросить",
        "в этот момент ваши данные",
        "как образом",
        "каким образом",
        "по дну океана",
    )
    return any(marker in prompt.lower() for marker in markers)


def _rebuild_image_prompt(shot: Shot) -> str:
    source = _clean_visual_prompt_source(
        shot.description or shot.asset_query or shot.screen_text or "a simple real scene"
    )
    if shot.asset_query and shot.asset_query.lower() not in source.lower():
        source = f"{source}, {shot.asset_query}"
    if shot.visual_type == "ai_typography_card":
        return (
            "Clean cinematic editorial title card, strong composition, high contrast, "
            "16:9 layout, minimal visual clutter. "
            f"Visible text, exactly: \"{shot.screen_text}\". Visual idea: {source}"
        )
    return (
        f"{_suggest_shot_type(source)} of {source}, a real recognizable place shown "
        "with clear shapes, colours and its own landmarks"
    )


def _suggest_motion(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("wide", "aerial", "establishing", "landscape", "overview", "panorama", "vista")):
        return "slowly drifts across the scene, letting the space and its landmarks read"
    if any(word in lower for word in ("close-up", "close up", "macro", "extreme", "detail")):
        return "gently pushes in on the detail and holds, keeping the subject stable"
    return "slowly pushes in and holds steady, letting the scene settle"


def _make_ltx_prompt(image_prompt: str) -> str:
    return (
        f"The camera {_suggest_motion(image_prompt)}. "
        "Subtle handmade movement brings the shot to life while the composition stays stable. "
        "Small environmental motion is visible where appropriate: light shifts, particles "
        "drift, fabric or water moves softly."
    )


def _repair_shot_prompts(shots: list[Shot]) -> None:
    for shot in shots:
        if shot.image_model and _prompt_has_too_much_transcript(shot.image_prompt):
            shot.image_prompt = _rebuild_image_prompt(shot)
        if shot.video_model and _prompt_has_too_much_transcript(shot.video_prompt):
            shot.video_prompt = _make_ltx_prompt(shot.image_prompt)


def is_concrete_prompt(text: str) -> bool:
    value = (text or "").strip().lower()
    if not value:
        return True
    return len(value.split()) >= 6 and not any(marker in value for marker in _ABSTRACT_MARKERS)


def auto_overlay_for_screen_text(text: str) -> bool:
    phrase = (text or "").strip()
    if not phrase or not any(char.isdigit() for char in phrase):
        return False
    words = phrase.split()
    return 1 <= len(words) <= 7 and len(phrase) <= 100


def choose_text_overlay_layout(text: str, shot: Shot) -> tuple[str, str, float, float]:
    phrase = (text or "").strip()
    words = phrase.split()
    lower = f"{phrase} {shot.description} {shot.visual_type} {shot.asset_query}".lower()
    index = int(getattr(shot, "index", 0) or 0)

    def side_pair(first: str, second: str) -> str:
        return first if index % 2 else second

    is_number = any(char.isdigit() for char in phrase)
    letters = [char for char in phrase if char.isalpha()]
    is_upper = bool(letters) and sum(char.isupper() for char in letters) / len(letters) >= 0.55
    is_error = any(word in lower for word in (
        "error", "warning", "failed", "failure", "limit", "throttle", "slow", "latency",
        "ошиб", "лимит", "предел", "сбой", "медлен", "задерж",
    ))
    is_terminal = any(word in lower for word in (
        "terminal", "console", "command", "cli", "code", "screen", "monitor", "benchmark",
        "тэрминал", "терминал", "команд", "код", "экран", "монитор", "бенчмарк",
    ))
    is_hands_or_desk = any(word in lower for word in (
        "hand", "hands", "keyboard", "desk", "laptop", "workstation", "рук", "клавиатур",
        "стол", "ноутбук", "рабоч",
    ))
    is_wide = any(word in lower for word in (
        "wide", "establishing", "landscape", "server rows", "data center", "datacenter",
        "общий", "панорам", "датацентр", "серверн",
    ))
    if is_error:
        return "warning", "top", 50.0, 12.0
    if is_number:
        return "stat", side_pair("top_left", "top_right"), 12.0 if index % 2 else 88.0, 16.0
    if is_terminal and len(words) <= 4:
        return "terminal", side_pair("top_right", "top_left"), 88.0 if index % 2 else 12.0, 14.0
    if is_upper or len(words) <= 3:
        return "badge", side_pair("top_left", "top_right"), 12.0 if index % 2 else 88.0, 14.0
    if is_hands_or_desk:
        return "caption", side_pair("top_left", "top_right"), 12.0 if index % 2 else 88.0, 18.0
    if is_wide:
        return "subtitle", side_pair("bottom_left", "bottom_right"), 12.0 if index % 2 else 88.0, 86.0
    positions = ("bottom", "top_left", "top_right", "bottom_left", "bottom_right")
    return "subtitle", positions[index % len(positions)], 50.0, 86.0


def determine_asset_route(shot: Shot, remotion_template: str = "") -> str:
    del remotion_template  # Kept for compatibility with the former GUI API.
    visual_type = (shot.visual_type or "").lower()
    if visual_type == "stock_video":
        return "stock"
    if visual_type == "still_kenburns":
        return "kenburns"
    if visual_type == "web_reference_card":
        return "reference"
    return "image+video" if shot.video_model else "image"


def sync_asset_card_route(card: AssetCard, image_preference: str = "Auto") -> AssetCard:
    """Keep the user-facing route and generation fields consistent."""
    route = (card.route or "").strip().lower()
    visual_type = (card.visual_type or "").strip().lower()
    if route not in {"stock", "kenburns", "reference", "image", "image+video"}:
        if visual_type == "stock_video":
            route = "stock"
        elif visual_type == "still_kenburns":
            route = "kenburns"
        elif visual_type == "web_reference_card":
            route = "reference"
        elif visual_type == "ai_image" or not card.video_model:
            route = "image"
        else:
            route = "image+video"

    preferred_model = preferred_realistic_image_model(image_preference)
    if route == "reference":
        card.route = "reference"
        card.visual_type = "web_reference_card"
        card.image_model = ""
        card.video_model = ""
    elif route == "stock":
        card.route = "stock"
        card.visual_type = "stock_video"
        card.image_model = ""
        card.video_model = ""
    elif route == "kenburns":
        card.route = "kenburns"
        card.visual_type = "still_kenburns"
        card.image_model = card.image_model or preferred_model
        card.video_model = ""
    elif route == "image":
        card.route = "image"
        card.visual_type = "ai_image"
        card.image_model = card.image_model or preferred_model
        card.video_model = ""
    else:
        card.route = "image+video"
        card.visual_type = "ai_image_then_i2v"
        card.image_model = card.image_model or preferred_model
        card.video_model = card.video_model or "ltx2.3_i2v"
    return card


def find_existing_asset(project_dir: Path | str, folder: str, shot_index: int) -> str:
    base = Path(project_dir) / "assets" / folder
    if not base.exists():
        return ""
    matches = sorted(base.glob(f"shot_{shot_index:03d}*"))
    return str(matches[0]) if matches else ""


def load_asset_board(project_dir: Path | str) -> list[AssetCard]:
    """Load the board while upgrading missing in-memory legacy defaults.

    Missing ``card_id`` values receive deterministic UUIDs.  Loading remains
    read-only; IDs are persisted by the next ordinary :func:`write_asset_board`.
    """
    project_dir = Path(project_dir)
    path = project_dir / ASSET_BOARD_FILE
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Asset board must contain a JSON array: {path}")

    cards: list[AssetCard] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Asset board row {index} must be a JSON object: {path}")
        card_id = str(item.get("card_id", "") or "").strip()
        if not card_id:
            card_id = legacy_asset_card_id(project_dir, index, item.get("shot_index", ""))
        data = {**_LEGACY_FIELD_DEFAULTS, **item, "card_id": card_id}
        cards.append(sync_asset_card_route(AssetCard(**data)))
    return cards


def write_asset_board(project_dir: Path | str, cards: list[AssetCard]) -> None:
    """Atomically persist a board and mark the assets pipeline stage current."""
    project_dir = Path(project_dir)

    # Bind v1 index-only provenance before a reorder makes those indices
    # ambiguous.  This is idempotent for schema-v2 manifests.
    existing = load_asset_board(project_dir)
    asset_variants.bind_card_identities(
        project_dir,
        {index: card.card_id for index, card in enumerate(existing)},
    )

    seen_ids: set[str] = set()
    for card in cards:
        card_id = str(getattr(card, "card_id", "") or "").strip()
        if not card_id or card_id in seen_ids:
            card_id = str(uuid.uuid4())
            card.card_id = card_id
        seen_ids.add(card_id)

    board_data = [asdict(card) for card in cards]
    atomic_write_text(
        project_dir / ASSET_BOARD_FILE,
        json.dumps(board_data, ensure_ascii=False, indent=2),
    )
    project = open_project(project_dir)
    if project is not None:
        project.mark_stage_completed(
            "assets",
            board_data,
            input_hash=project.content_hash(project.read_json("shot_list") or []),
        )


def build_asset_board(
    project_dir: Path | str,
    shots: list[Shot] | None = None,
) -> list[AssetCard]:
    """Build/reconcile cards from scenes and shots without discarding edits."""
    project_dir = Path(project_dir)
    scenes_path = project_dir / "02_scenes.json"
    if not scenes_path.exists():
        raise FileNotFoundError(f"Не найден файл сцен: {scenes_path}")
    if shots is None:
        shots_path = project_dir / "04_shot_list.json"
        if not shots_path.exists():
            raise FileNotFoundError(f"Не найден shot list: {shots_path}")
        shots = [Shot(**item) for item in json.loads(shots_path.read_text(encoding="utf-8"))]

    scenes = [Scene(**item) for item in json.loads(scenes_path.read_text(encoding="utf-8"))]
    _normalize_shot_routes(shots)
    _repair_shot_prompts(shots)
    scene_titles = {scene.index: scene.title for scene in scenes}
    scene_voice = {scene.index: scene.voice_text for scene in scenes}
    existing_by_shot = {card.shot_index: card for card in load_asset_board(project_dir)}
    cards: list[AssetCard] = []

    for shot in shots:
        candidate = existing_by_shot.get(shot.index)
        old = (
            candidate
            if candidate is not None and _card_matches_shot_plan(candidate, shot)
            else None
        )
        plan_fingerprint = media_plan_fingerprint(shot)
        route = old.route if old and old.route else determine_asset_route(shot)
        image_model = old.image_model if old and old.image_model else shot.image_model
        video_model = old.video_model if old and old.video_model else shot.video_model
        # Only discover loose legacy files when no board row existed at all.
        # If a row existed but its semantic fingerprint changed, matching
        # shot_XXX files belong to the rejected old plan and must stay orphaned.
        image_output = (
            old.image_output
            if old
            else (
                find_existing_asset(project_dir, "images", shot.index)
                if candidate is None
                else ""
            )
        )
        video_output = (
            old.video_output
            if old
            else (
                find_existing_asset(project_dir, "videos", shot.index)
                if candidate is None
                else ""
            )
        )
        image_prompt = old.image_prompt if old and old.image_prompt else shot.image_prompt

        if old and (old.status or "").lower() not in {"planned", "needs_review", ""}:
            status, notes = old.status, old.notes
        elif not is_concrete_prompt(shot.description):
            status = "needs_review"
            notes = "Возможно слишком абстрактное/метафоричное описание сцены — проверьте."
        else:
            status, notes = "planned", ""

        source_text = scene_voice.get(shot.scene_index, "")
        old_screen_text = normalize_numeric_screen_text(
            source_text,
            old.screen_text if old else "",
        )
        shot_screen_text = normalize_numeric_screen_text(source_text, shot.screen_text)
        screen_text = old_screen_text or shot_screen_text
        if old is not None:
            overlay_enabled = old.text_overlay_enabled
            explicit_overlay = normalize_numeric_screen_text(
                source_text,
                old.text_overlay_text,
            )
            if overlay_enabled:
                overlay_enabled = auto_overlay_for_screen_text(
                    explicit_overlay or screen_text
                )
            if overlay_enabled and not explicit_overlay:
                overlay_style, overlay_position, overlay_x, overlay_y = choose_text_overlay_layout(
                    screen_text,
                    shot,
                )
            else:
                overlay_style = old.text_overlay_style
                overlay_position = old.text_overlay_position
                overlay_x = old.text_overlay_x
                overlay_y = old.text_overlay_y
            overlay_text = explicit_overlay
            overlay_enter = old.text_overlay_enter
            overlay_exit = old.text_overlay_exit
        else:
            overlay_enabled = auto_overlay_for_screen_text(screen_text)
            overlay_style, overlay_position, overlay_x, overlay_y = choose_text_overlay_layout(
                screen_text,
                shot,
            )
            overlay_text = ""
            overlay_enter = "fade"
            overlay_exit = "fade"

        card = AssetCard(
            card_id=old.card_id if old else str(uuid.uuid4()),
            shot_index=shot.index,
            scene_index=shot.scene_index,
            scene_title=scene_titles.get(shot.scene_index, f"Scene {shot.scene_index}"),
            start=shot.start,
            end=shot.end,
            visual_type=shot.visual_type,
            route=route,
            description=shot.description,
            asset_query=shot.asset_query,
            image_model=image_model,
            video_model=video_model,
            remotion_template=old.remotion_template if old else "",
            image_prompt=image_prompt,
            video_prompt=old.video_prompt if old and old.video_prompt else shot.video_prompt,
            screen_text=screen_text,
            motion=shot.motion,
            fallback=shot.fallback,
            image_output=image_output,
            video_output=video_output,
            remotion_output=old.remotion_output if old else "",
            status=status,
            notes=notes,
            approved=old.approved if old else False,
            timeline_start=old.timeline_start if old else "",
            timeline_end=old.timeline_end if old else "",
            assigned_media=old.assigned_media if old else "",
            track=old.track if old else "video_1",
            text_overlay_enabled=overlay_enabled,
            text_overlay_text=overlay_text,
            text_overlay_position=overlay_position,
            text_overlay_style=overlay_style,
            text_overlay_x=overlay_x,
            text_overlay_y=overlay_y,
            text_overlay_enter=overlay_enter,
            text_overlay_exit=overlay_exit,
            source_url=old.source_url if old else "",
            source_license=old.source_license if old else "",
            source_author=old.source_author if old else "",
            stock_score=old.stock_score if old else 0.0,
            sfx_query=old.sfx_query if old and old.sfx_query else shot.sfx_query,
            sfx_output=old.sfx_output if old else "",
            sfx_accent_query=(
                old.sfx_accent_query
                if old and old.sfx_accent_query
                else getattr(shot, "sfx_accent_query", "") or ""
            ),
            sfx_accent_output=old.sfx_accent_output if old else "",
            priority=shot.priority or "medium",
            qa_score=old.qa_score if old else -1.0,
            ltx_draft=old.ltx_draft if old else False,
            reference_query=getattr(shot, "reference_query", "") or "",
            reference_source=getattr(shot, "reference_source", "") or "wikipedia",
            reference_url=old.reference_url if old else "",
            reference_output=old.reference_output if old else "",
            reference_credit=old.reference_credit if old else "",
            plan_fingerprint=plan_fingerprint,
            purpose=shot.purpose,
            continuity_mode=shot.continuity_mode,
            continuity_group_id=shot.continuity_group_id,
            location_id=shot.location_id,
            subject_ids=list(shot.subject_ids),
            state_before=shot.state_before,
            action=shot.action,
            state_after=shot.state_after,
            shot_size=shot.shot_size,
            camera_angle=shot.camera_angle,
            screen_direction=shot.screen_direction,
            reference_frame_id=shot.reference_frame_id,
            transition_in=shot.transition_in,
            transition_out=shot.transition_out,
            change_from_previous=shot.change_from_previous,
        )
        cards.append(sync_asset_card_route(card))

    previous_text = ""
    for card in cards:
        if not card.text_overlay_enabled:
            previous_text = ""
            continue
        text = (card.text_overlay_text or card.screen_text or "").strip().lower()
        if text and text == previous_text:
            card.text_overlay_enabled = False
        else:
            previous_text = text
    return cards


__all__ = [
    "ASSET_BOARD_FILE",
    "auto_overlay_for_screen_text",
    "build_asset_board",
    "choose_text_overlay_layout",
    "determine_asset_route",
    "find_existing_asset",
    "is_concrete_prompt",
    "load_asset_board",
    "media_plan_fingerprint",
    "preferred_realistic_image_model",
    "sync_asset_card_route",
    "write_asset_board",
]
