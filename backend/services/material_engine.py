"""Server-side unattended material production.

This module is deliberately HTTP-agnostic.  The assets router supplies its
existing provider-aware generation function; the engine supplies durable
planning, bounded retries, progress, crash checkpoints and a deterministic
gap-free fallback.
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont

from backend.services import jobs
from core import board as board_store
from core import canonical_references
from core import comfy_generation
from core import kenburns
from core import media_observations
from core.config import load_settings
from core.export import media_duration_seconds
from core.models import AssetCard, timecode_to_seconds
from core.production import (
    ProductionPolicy,
    build_material_operations,
    card_is_ready,
    freeze_material_plan,
    load_material_plan,
    load_material_run,
    material_plan_matches_project,
    new_material_run,
    preflight_material_plan,
    save_material_run,
    transition_material_run,
    utc_now,
)


GenerateCard = Callable[..., dict[str, Any]]
ServiceCheck = Callable[[str], bool]


class MaterialPreflightError(ValueError):
    def __init__(self, preflight: dict[str, Any]):
        self.preflight = preflight
        blockers = [
            issue.get("message", "")
            for issue in preflight.get("issues", [])
            if issue.get("level") == "blocker"
        ]
        super().__init__("; ".join(filter(None, blockers)) or "Material preflight failed")


class MaterialDependencyUnavailable(RuntimeError):
    """Pause a resumable run instead of converting a systemic outage to fallbacks."""


class MaterialSystemicProviderError(RuntimeError):
    """Stop a run when the same operation proves the provider itself is broken."""


_SYSTEMIC_PROVIDER_MARKERS = (
    "cuda out of memory",
    "out of memory",
    "cannot allocate memory",
    "missing_node_type",
    "workflow file not found",
    "model file not found",
    "canonical reference unavailable",
    "reference workflow",
)


def _is_systemic_provider_error(error: BaseException) -> bool:
    message = str(error).casefold()
    return any(marker in message for marker in _SYSTEMIC_PROVIDER_MARKERS)


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts") / ("arialbd.ttf" if bold else "arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu")
        / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental")
        / ("Arial Bold.ttf" if bold else "Arial.ttf"),
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return ImageFont.truetype(str(candidate), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fallback_still(project_dir: Path, card: AssetCard, reason: str) -> Path:
    """Render a deterministic, legible safety card when every provider failed."""

    width, height = 1920, 1080
    image = Image.new("RGB", (width, height), "#101827")
    draw = ImageDraw.Draw(image)
    # A restrained gradient keeps the card usable under documentary footage
    # without pretending to be a successful generative image.
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = (
            int(15 + 16 * ratio),
            int(23 + 17 * ratio),
            int(42 + 31 * ratio),
        )
        draw.line((0, y, width, y), fill=color)
    draw.rounded_rectangle(
        (150, 190, width - 150, height - 190),
        radius=42,
        fill=(15, 23, 42, 235),
        outline=(99, 102, 241),
        width=4,
    )
    title_font = _font(70, bold=True)
    body_font = _font(38)
    meta_font = _font(28)
    title = str(card.scene_title or f"Сцена {card.scene_index}").strip()
    description = str(card.description or "").strip()
    title_lines = textwrap.wrap(title, width=34)[:3] or [f"Сцена {card.scene_index}"]
    body_lines = textwrap.wrap(description, width=72)[:7]
    y = 285
    for line in title_lines:
        draw.text((235, y), line, font=title_font, fill="#F8FAFC")
        y += 88
    y += 28
    for line in body_lines:
        draw.text((235, y), line, font=body_font, fill="#CBD5E1")
        y += 55
    draw.text(
        (235, height - 270),
        f"Кадр {card.shot_index} · безопасный резервный материал",
        font=meta_font,
        fill="#A5B4FC",
    )
    destination = (
        project_dir
        / "assets"
        / "images"
        / f"shot_{card.shot_index:03d}_unattended_fallback.png"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG")
    return destination


def _apply_kenburns_fallback(
    project_dir: Path,
    card_index: int,
    *,
    ffmpeg: str,
    reason: str,
) -> AssetCard:
    cards = board_store.load_asset_board(project_dir)
    if not 0 <= card_index < len(cards):
        raise IndexError(f"Asset card {card_index} no longer exists")
    card = cards[card_index]
    source = Path(str(card.image_output or ""))
    image_check = (
        media_observations.observe_image(source)
        if str(card.image_output or "").strip()
        else {"ok": False}
    )
    if not image_check.get("ok"):
        source = _fallback_still(project_dir, card, reason)
        card.image_output = str(source)

    duration = max(
        2.0,
        float(timecode_to_seconds(card.end) - timecode_to_seconds(card.start)),
    )
    destination = (
        project_dir
        / "assets"
        / "videos"
        / f"shot_{card.shot_index:03d}_unattended_fallback.mp4"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        kenburns.build_kenburns_clip(
            source,
            destination,
            ffmpeg or "ffmpeg",
            duration,
            direction=kenburns.pick_variant(card.shot_index),
            jitter=False,
        )
    except Exception as exc:
        # A broken/missing ffmpeg must not turn an otherwise usable still into
        # a gap. Export already supports image-only cards, so this is the final
        # dependency-free safety tier.
        card.video_output = ""
        card.visual_type = "ai_image"
        card.route = "image"
        note = f"Ken Burns unavailable → fallback still ({exc})"
    else:
        card.video_output = str(destination)
        card.visual_type = "still_kenburns"
        card.route = "kenburns"
        note = f"Unattended fallback → Ken Burns ({reason})"
    card.status = "generated"
    card.approved = False
    card.qa_score = -1.0
    card.notes = f"{card.notes}\n{note}".strip()
    board_store.write_asset_board(project_dir, cards)
    return card


def _operation_plan(
    cards: list[AssetCard],
    *,
    skip_ready: bool = True,
) -> list[dict[str, Any]]:
    """Group work by provider to avoid repeated VRAM model swaps."""

    return build_material_operations(
        [card.__dict__ for card in cards],
        skip_ready=skip_ready,
    )


def _resolve_card(project_dir: Path, card_id: str) -> tuple[int, AssetCard]:
    cards = board_store.load_asset_board(project_dir)
    for index, card in enumerate(cards):
        if card.card_id == card_id:
            return index, card
    raise RuntimeError("Asset Board changed during unattended production")


def _operation_already_done(card: AssetCard, operation: dict[str, Any]) -> bool:
    mode = operation["mode"]
    if mode == "image":
        return bool(card.image_output and Path(card.image_output).is_file())
    if mode in {"video", "route"}:
        if mode == "route" and str(card.visual_type or "").lower() == "ai_image":
            return bool(card.image_output and Path(card.image_output).is_file())
        return bool(
            (card.assigned_media and Path(card.assigned_media).is_file())
            or (card.video_output and Path(card.video_output).is_file())
        )
    return card_is_ready(card.__dict__)


def _validate_operation(
    card: AssetCard,
    operation: dict[str, Any],
    *,
    ffmpeg: str,
    enabled: bool,
) -> None:
    if not enabled:
        return
    if operation["kind"] == "image":
        result = media_observations.observe_image(Path(card.image_output))
        if not result.get("ok"):
            raise RuntimeError("Сгенерированное изображение не читается")
        return
    video = Path(card.assigned_media or card.video_output)
    if not video.is_file() or video.stat().st_size <= 0:
        raise RuntimeError("Сгенерированное видео отсутствует или пустое")
    if media_duration_seconds(video, ffmpeg or "ffmpeg") <= 0:
        raise RuntimeError("Сгенерированное видео не имеет корректной длительности")


def _card_run_state(run: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    cards = run.setdefault("cards", {})
    return cards.setdefault(
        operation["card_id"],
        {
            "card_id": operation["card_id"],
            "card_index": operation["card_index"],
            "shot_index": operation["shot_index"],
            "status": "pending",
            "attempts": {"image": 0, "video": 0},
            "fallback": False,
            "error": "",
        },
    )


def _reference_snapshot_matches_current(plan: dict[str, Any]) -> bool:
    """Prevent resume with a different conditioning workflow or policy."""

    if int(plan.get("schema_version", 0) or 0) < 3:
        return True
    frozen = plan.get("reference_conditioning", {})
    if not isinstance(frozen, dict):
        return False
    current = comfy_generation.reference_workflow_status(load_settings())
    if str(frozen.get("mode", "auto")) != str(current.get("mode", "auto")):
        return False
    if bool(frozen.get("enabled")) != bool(current.get("enabled")):
        return False
    if frozen.get("enabled"):
        return str(frozen.get("workflow_sha256", "")) == str(
            current.get("workflow_sha256", "")
        )
    return True


def run_unattended_materials(
    project_dir: Path | str,
    project_id: str,
    policy: ProductionPolicy,
    cancel_event,
    generate_card: GenerateCard,
    service_check: ServiceCheck | None = None,
    dependency_wait_timeout: float | None = None,
) -> dict[str, Any]:
    """Run or resume a complete material queue under one durable project job."""

    root = Path(project_dir)
    preflight = preflight_material_plan(root)
    preflight_payload = preflight.to_dict()
    if not preflight.allowed:
        raise MaterialPreflightError(preflight_payload)

    existing = load_material_run(root)
    frozen_plan = load_material_plan(root)
    resumable = bool(
        existing
        and frozen_plan
        and existing.get("status") in {"queued", "running", "interrupted"}
        and existing.get("plan_hash") == frozen_plan.get("plan_hash")
        and material_plan_matches_project(root, frozen_plan)
        and _reference_snapshot_matches_current(frozen_plan)
        and policy.auto_resume
    )
    if resumable:
        run = existing
        run["policy"] = policy.__dict__.copy()
        run["error"] = ""
    else:
        frozen_plan = freeze_material_plan(root, preflight, policy)
        run = new_material_run(root, project_id, preflight, policy)

    transition_material_run(run, "running")
    run["started_at"] = run.get("started_at") or utc_now()
    run["finished_at"] = None
    save_material_run(root, run)

    try:
        cards = board_store.load_asset_board(root)
        operations = [dict(operation) for operation in frozen_plan["operations"]]
        for operation in operations:
            operation.setdefault(
                "operation_id",
                f"{operation.get('phase', 'other')}:{operation.get('card_id', '')}",
            )
            operation.setdefault("depends_on", [])
        summary = run.setdefault("summary", {})
        summary["cards_total"] = len(cards)
        summary["operations_total"] = len(operations)
        summary["operations_completed"] = 0
        summary.setdefault("retries", 0)
        summary.setdefault("fallbacks", 0)
        summary.setdefault("failures", 0)
        completed = 0
        completed_operation_ids: set[str] = set()
        reference_conditioning_enabled = bool(
            frozen_plan.get("reference_conditioning", {}).get("enabled")
        )

        # Fallback rendering uses the same configured binary as the ordinary
        # generator/export path.
        ffmpeg = str(load_settings().ffmpeg or "ffmpeg")

        for operation in operations:
            jobs.check(cancel_event)
            missing_dependencies = [
                operation_id
                for operation_id in operation.get("depends_on", [])
                if operation_id not in completed_operation_ids
            ]
            if missing_dependencies:
                raise MaterialSystemicProviderError(
                    "Нарушен замороженный граф операций; не завершены зависимости: "
                    + ", ".join(missing_dependencies)
                )
            card_index, card = _resolve_card(root, operation["card_id"])
            operation["card_index"] = card_index
            card_state = _card_run_state(run, operation)
            run["current"] = {
                "phase": operation["phase"],
                "card_id": card.card_id,
                "card_index": card_index,
                "shot_index": card.shot_index,
                "mode": operation["mode"],
            }

            already_done = policy.skip_ready and _operation_already_done(
                card, operation
            )
            if already_done:
                try:
                    _validate_operation(
                        card,
                        operation,
                        ffmpeg=ffmpeg,
                        enabled=policy.technical_validation,
                    )
                except Exception as exc:
                    # A crash may leave a path pointing at a partial/corrupt
                    # file. Treat it as pending work, not a completed checkpoint.
                    card_state["status"] = "pending"
                    card_state["error"] = f"Повреждён промежуточный файл: {exc}"
                    save_material_run(root, run)
                else:
                    if operation.get("reference_role") == "anchor":
                        canonical_references.register_reference(
                            root,
                            reference_frame_id=operation["reference_frame_id"],
                            anchor_card_id=card.card_id,
                            anchor_shot_index=card.shot_index,
                            image_path=card.image_output,
                        )
                    card_state["status"] = "ready"
                    completed += 1
                    completed_operation_ids.add(operation["operation_id"])
                    summary["operations_completed"] = completed
                    save_material_run(root, run)
                    jobs.update_progress(
                        cancel_event,
                        completed / max(1, len(operations)),
                        f"Кадр {card.shot_index}: уже готов",
                    )
                    continue

            attempts = (
                policy.max_image_attempts
                if operation["kind"] == "image"
                else policy.max_video_attempts
            )
            attempt_state = card_state.setdefault("attempts", {})
            previous_attempts = min(
                attempts,
                max(0, int(attempt_state.get(operation["kind"], 0) or 0)),
            )
            error: BaseException | None = (
                RuntimeError(
                    str(card_state.get("error", "") or "Лимит попыток исчерпан")
                )
                if previous_attempts >= attempts
                else None
            )
            reference_image: Path | None = None
            if (
                reference_conditioning_enabled
                and operation.get("reference_role") == "dependent"
            ):
                reference_image = canonical_references.resolve_reference(
                    root,
                    operation["reference_frame_id"],
                    expected_anchor_card_id=operation["reference_anchor_card_id"],
                )
                if reference_image is None:
                    # Recover the registry deterministically after a process
                    # crash that happened between board save and registry save.
                    _, anchor_card = _resolve_card(
                        root,
                        operation["reference_anchor_card_id"],
                    )
                    anchor_run_state = run.get("cards", {}).get(
                        operation["reference_anchor_card_id"],
                        {},
                    )
                    if (
                        not anchor_run_state.get("fallback")
                        and anchor_card.image_output
                        and Path(anchor_card.image_output).is_file()
                    ):
                        canonical_references.register_reference(
                            root,
                            reference_frame_id=operation["reference_frame_id"],
                            anchor_card_id=anchor_card.card_id,
                            anchor_shot_index=anchor_card.shot_index,
                            image_path=anchor_card.image_output,
                        )
                        reference_image = canonical_references.resolve_reference(
                            root,
                            operation["reference_frame_id"],
                            expected_anchor_card_id=operation[
                                "reference_anchor_card_id"
                            ],
                        )
                if reference_image is None:
                    raise MaterialSystemicProviderError(
                        "Canonical reference unavailable for "
                        f"{operation['reference_frame_id']}"
                    )
            for attempt in range(previous_attempts + 1, attempts + 1):
                jobs.check(cancel_event)
                if (
                    service_check is not None
                    and operation["phase"] in {"images", "videos", "other"}
                    and not service_check("comfy")
                ):
                    dependency_error = (
                        "ComfyUI недоступен; очередь приостановлена и продолжится "
                        "после восстановления сервиса."
                    )
                    transition_material_run(
                        run,
                        "interrupted",
                        reason="ComfyUI unavailable",
                    )
                    run["error"] = dependency_error
                    save_material_run(root, run)
                    jobs.update_progress(
                        cancel_event,
                        completed / max(1, len(operations)),
                        "Ожидание ComfyUI",
                    )
                    if dependency_wait_timeout is not None:
                        deadline = time.monotonic() + max(
                            0.0, float(dependency_wait_timeout)
                        )
                    else:
                        deadline = None
                    while not service_check("comfy"):
                        jobs.check(cancel_event)
                        if deadline is not None and time.monotonic() >= deadline:
                            raise MaterialDependencyUnavailable(dependency_error)
                        wait_seconds = 5.0
                        if deadline is not None:
                            wait_seconds = min(
                                wait_seconds,
                                max(0.01, deadline - time.monotonic()),
                            )
                        if hasattr(cancel_event, "wait"):
                            cancel_event.wait(wait_seconds)
                        else:
                            time.sleep(wait_seconds)
                    transition_material_run(
                        run,
                        "running",
                        reason="ComfyUI restored",
                    )
                    run["error"] = ""
                    save_material_run(root, run)
                card_state["status"] = "generating"
                attempt_state[operation["kind"]] = attempt
                run["current"]["attempt"] = attempt
                save_material_run(root, run)
                jobs.update_progress(
                    cancel_event,
                    completed / max(1, len(operations)),
                    (
                        f"{operation['phase']}: кадр {card.shot_index}, "
                        f"попытка {attempt}/{attempts}"
                    ),
                )
                try:
                    result = generate_card(
                        root,
                        card_index,
                        operation["mode"],
                        False,
                        # An unattended run is the one-pass production action:
                        # do not leave an LTX draft that needs a later manual
                        # "Финал" click. Manual generation still honours the
                        # configured draft policy.
                        operation["phase"] == "videos",
                        cancel_check=lambda: jobs.check(cancel_event),
                        defer_qa=True,
                        reference_image=reference_image,
                    )
                    result_card = result.get("card") if isinstance(result, dict) else None
                    if isinstance(result_card, dict):
                        card = AssetCard(**result_card)
                    else:
                        _, card = _resolve_card(root, operation["card_id"])
                    _validate_operation(
                        card,
                        operation,
                        ffmpeg=ffmpeg,
                        enabled=policy.technical_validation,
                    )
                    error = None
                    if operation.get("reference_role") == "anchor":
                        canonical_references.register_reference(
                            root,
                            reference_frame_id=operation["reference_frame_id"],
                            anchor_card_id=card.card_id,
                            anchor_shot_index=card.shot_index,
                            image_path=card.image_output,
                        )
                    card_state["status"] = "ready"
                    card_state["error"] = ""
                    break
                except jobs.JobCancelled:
                    raise
                except BaseException as exc:
                    error = exc
                    card_state["error"] = str(exc)
                    if attempt < attempts:
                        summary["retries"] = int(summary.get("retries", 0)) + 1
                    elif _is_systemic_provider_error(exc):
                        card_state["status"] = "provider_blocked"
                        summary["failures"] = int(summary.get("failures", 0)) + 1
                        save_material_run(root, run)
                        raise MaterialSystemicProviderError(
                            "Провайдер материалов системно неработоспособен; "
                            f"очередь остановлена без массового fallback: {exc}"
                        ) from exc
                    save_material_run(root, run)

            if error is not None:
                summary["failures"] = int(summary.get("failures", 0)) + 1
                card_state["status"] = "fallback"
                save_material_run(root, run)
                fallback = _apply_kenburns_fallback(
                    root,
                    card_index,
                    ffmpeg=ffmpeg,
                    reason=str(error),
                )
                card_state["status"] = "ready"
                card_state["fallback"] = True
                card_state["error"] = str(error)
                card_state["fallback_output"] = (
                    fallback.video_output or fallback.image_output
                )
                summary["fallbacks"] = int(summary.get("fallbacks", 0)) + 1

            completed += 1
            completed_operation_ids.add(operation["operation_id"])
            summary["operations_completed"] = completed
            save_material_run(root, run)
            jobs.update_progress(
                cancel_event,
                completed / max(1, len(operations)),
                f"Материалы: {completed} из {len(operations)} операций",
            )

        final_cards = board_store.load_asset_board(root)
        missing = [
            card.shot_index
            for card in final_cards
            if str(card.status or "").lower() != "skipped"
            and not card_is_ready(card.__dict__)
        ]
        if missing:
            raise RuntimeError(
                "Unattended run finished with missing media for shots: "
                + ", ".join(map(str, missing))
            )
        summary["cards_ready"] = sum(card_is_ready(card.__dict__) for card in final_cards)
        run["current"] = {}
        transition_material_run(run, "succeeded")
        run["finished_at"] = utc_now()
        save_material_run(root, run)
        jobs.update_progress(cancel_event, 1.0, "Все материалы готовы")
        return run
    except jobs.JobCancelled:
        transition_material_run(
            run,
            "cancelled",
            reason="Cancelled by user",
        )
        run["finished_at"] = utc_now()
        run["error"] = "Задача отменена пользователем"
        save_material_run(root, run)
        raise
    except MaterialDependencyUnavailable as exc:
        transition_material_run(
            run,
            "interrupted",
            reason="Dependency unavailable",
        )
        run["finished_at"] = None
        run["error"] = str(exc)
        save_material_run(root, run)
        raise
    except BaseException as exc:
        transition_material_run(
            run,
            "failed",
            reason=type(exc).__name__,
        )
        run["finished_at"] = utc_now()
        run["error"] = str(exc)
        save_material_run(root, run)
        raise


__all__ = [
    "MaterialDependencyUnavailable",
    "MaterialPreflightError",
    "MaterialSystemicProviderError",
    "run_unattended_materials",
]
