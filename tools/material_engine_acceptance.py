"""Real end-to-end acceptance harness for unattended material production.

The harness always works in a dedicated project directory.  It can terminate
its own process after a completed provider call to emulate a desktop/backend
crash without stopping ComfyUI.  A second invocation then resumes from the
durable material checkpoint and validates the generated media.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.routers import assets
from backend.services import jobs
from backend.services.material_engine import run_unattended_materials
from core import continuity
from core.board import build_asset_board, load_asset_board, write_asset_board
from core.config import load_settings
from core.models import Scene, Shot, TranscriptSegment
from core.production import (
    ProductionPolicy,
    card_is_ready,
    load_material_plan,
    load_material_run,
    preflight_material_plan,
)
from core.project import ProjectExists, atomic_write_text, content_hash, create_project


def _write_stage(project, key: str, stage: str, payload: list[dict[str, Any]]) -> None:
    project.write_json(key, payload)
    project.mark_stage_completed(stage, payload)


def prepare_project(project_dir: Path) -> dict[str, Any]:
    """Create a tiny but representative three-route production project."""

    settings = load_settings()
    expected_root = Path(settings.output_base or "projects").expanduser().resolve()
    project_dir = project_dir.expanduser().resolve()
    if project_dir.parent != expected_root:
        raise ValueError(
            f"Acceptance project must be a direct child of {expected_root}"
        )
    if project_dir.exists():
        raise ProjectExists(f"Refusing to reuse existing directory: {project_dir}")

    project = create_project(
        project_dir.name,
        expected_root,
        settings=settings,
        preset="universal",
    )
    project.metadata.update(
        {
            "video_style": "documentary",
            "shot_mode": "ai",
            "route_priority": "balanced",
            "detected_language": "ru",
            "music_enabled": False,
        }
    )
    project.save_meta()

    transcript = [
        TranscriptSegment(
            start="00:00:00",
            end="00:00:04",
            text="Локальный движок создаёт изображение.",
        ),
        TranscriptSegment(
            start="00:00:04",
            end="00:00:08",
            text="Второй кадр получает плавное движение камеры.",
        ),
        TranscriptSegment(
            start="00:00:08",
            end="00:00:12",
            text="После перезапуска очередь продолжает работу.",
        ),
    ]
    transcript_payload = [asdict(item) for item in transcript]
    _write_stage(
        project,
        "transcript_segments",
        "transcript",
        transcript_payload,
    )

    scenes = [
        Scene(
            index=1,
            start="00:00:00",
            end="00:00:12",
            title="Проверка автономного движка",
            voice_text=" ".join(item.text for item in transcript),
            visual_direction=(
                "Три конкретных производственных кадра: рабочая станция, "
                "механизм камеры и восстановленная очередь."
            ),
            screen_text="",
            music_note="",
            retention_note="Смена визуального действия каждые четыре секунды.",
        )
    ]
    scene_payload = [asdict(item) for item in scenes]
    project.write_json("scenes", scene_payload)
    project.mark_stage_completed(
        "scenes",
        scene_payload,
        input_hash=content_hash(transcript_payload),
    )

    shots = [
        Shot(
            index=1,
            scene_index=1,
            start="00:00:00",
            end="00:00:04",
            visual_type="ai_image",
            description=(
                "Крупный план настольной рабочей станции, на экране появляется "
                "готовая иллюстрация, рядом лежит блокнот."
            ),
            asset_query="настольная рабочая станция и готовая иллюстрация",
            image_model="krea2_turbo",
            video_model="",
            image_prompt=(
                "Close-up of a modern desktop workstation creating a finished "
                "cinematic illustration, monitor glow, notebook and pencil, "
                "realistic documentary photography, clean composition, no text, "
                "no logo, widescreen"
            ),
            video_prompt="",
            screen_text="",
            motion="Статичный выразительный кадр.",
            priority="medium",
            fallback="still_kenburns",
            segment_ids=[1],
            visual_concept_id="acceptance-workstation",
        ),
        Shot(
            index=2,
            scene_index=1,
            start="00:00:04",
            end="00:00:08",
            visual_type="still_kenburns",
            description=(
                "Камера на компактном слайдере плавно движется вдоль макета "
                "съёмочной площадки, видны направляющие и объектив."
            ),
            asset_query="камера на слайдере и макет съёмочной площадки",
            image_model="krea2_turbo",
            video_model="",
            image_prompt=(
                "Professional cinema camera on a compact slider beside a detailed "
                "miniature film set, visible rails and lens, warm practical light, "
                "realistic documentary photography, depth, no text, no logo, "
                "widescreen"
            ),
            video_prompt="",
            screen_text="",
            motion="Медленное приближение и боковой сдвиг.",
            priority="medium",
            fallback="still_kenburns",
            segment_ids=[2],
            visual_concept_id="acceptance-slider",
        ),
        Shot(
            index=3,
            scene_index=1,
            start="00:00:08",
            end="00:00:12",
            visual_type="ai_image_then_i2v",
            description=(
                "Ряд световых индикаторов производственной очереди последовательно "
                "загорается после восстановления питания."
            ),
            asset_query="световые индикаторы очереди после восстановления питания",
            image_model="krea2_turbo",
            video_model="ltx2.3_i2v",
            image_prompt=(
                "Macro view of a physical production queue controller with rows "
                "of small indicator lights powering back on, dark technical room, "
                "realistic cinematic documentary style, no readable text, no logo, "
                "widescreen"
            ),
            video_prompt=(
                "Indicator lights turn on one after another from left to right, "
                "subtle camera push-in, restrained mechanical vibration, realistic motion"
            ),
            screen_text="",
            motion="Индикаторы включаются по очереди, камера медленно приближается.",
            priority="medium",
            fallback="still_kenburns",
            segment_ids=[3],
            visual_concept_id="acceptance-resume",
        ),
    ]
    global_visual_plan = [
        {
            "scene_index": 1,
            "narrative_role": "Проверить полный автономный производственный цикл",
            "visual_family": "physical_process",
            "location": "Локальная студия автоматизированного видеопроизводства",
            "primary_subject": "Производственная система и её физические элементы",
            "shot_progression": "Рабочая станция → камера → индикаторы очереди",
            "palette": "Тёплый документальный свет с нейтральными техническими акцентами",
            "transition": "Причинный стык по завершению операции",
            "avoid": "Не повторять одну и ту же композицию",
            "story_thread": "Автономное производство и восстановление",
            "continuity_mode": "evidence_sequence",
            "location_id": "location-production-studio",
            "subject_ids": "subject-production-system",
            "entry_state": "Производственная система готова к запуску",
            "scene_event": "Система создаёт материалы и восстанавливает очередь",
            "exit_state": "Все операции завершены, очередь восстановлена",
        }
    ]
    continuity.apply_storyboard_context(shots, global_visual_plan)
    shot_payload = [asdict(item) for item in shots]
    atomic_write_text(
        project.dir / "03_global_visual_plan.json",
        json.dumps(global_visual_plan, ensure_ascii=False, indent=2),
    )
    project.write_json("shot_list", shot_payload)
    project.mark_stage_completed(
        "shots",
        shot_payload,
        input_hash=content_hash(scene_payload),
    )
    context_bible = continuity.build_context_bible(
        scenes,
        global_visual_plan,
        shots,
    )
    continuity.write_context_bible(project.dir, context_bible)

    cards = build_asset_board(project.dir, shots)
    # ``build_asset_board`` keeps legacy defaults that promote a plain AI still
    # to image+video.  Pin the acceptance routes explicitly so the graph covers
    # image-only, Ken Burns and image-to-video as three distinct paths.
    cards[0].route = "image"
    cards[0].visual_type = "ai_image"
    cards[0].video_model = ""
    cards[1].route = "kenburns"
    cards[1].visual_type = "still_kenburns"
    cards[1].video_model = ""
    cards[2].route = "image+video"
    cards[2].visual_type = "ai_image_then_i2v"
    cards[2].video_model = "ltx2.3_i2v"
    write_asset_board(project.dir, cards)
    preflight = preflight_material_plan(project.dir)
    workflow_preflight = assets._material_workflow_preflight(project.dir, settings)
    report = {
        "project_dir": str(project.dir),
        "context_bible": str(project.dir / continuity.CONTEXT_BIBLE_FILE),
        "preflight": preflight.to_dict(),
        "workflow_preflight": workflow_preflight,
    }
    if not preflight.allowed:
        raise RuntimeError(json.dumps(report, ensure_ascii=False, indent=2))
    if not workflow_preflight.get("ok"):
        raise RuntimeError(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def _comfy_available(service: str) -> bool:
    if service != "comfy":
        return True
    settings = load_settings()
    base = (settings.comfy_url or "http://127.0.0.1:8188").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/system_stats", timeout=3):
            return True
    except Exception:
        return False


def run_project(project_dir: Path, *, crash_after: int = 0) -> dict[str, Any]:
    """Run the real provider queue, optionally hard-exiting after N calls."""

    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise FileNotFoundError(project_dir)
    call_count = 0

    def generate(*args, **kwargs):
        nonlocal call_count
        result = assets._generate_sync(*args, **kwargs)
        call_count += 1
        if crash_after and call_count >= crash_after:
            marker = {
                "schema_version": 1,
                "at_epoch": time.time(),
                "provider_calls_completed": call_count,
                "message": "Intentional acceptance crash after persisted provider output",
            }
            (project_dir / ".acceptance_crash.json").write_text(
                json.dumps(marker, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(
                f"INTENTIONAL_CRASH provider_calls_completed={call_count}",
                flush=True,
            )
            os._exit(86)
        return result

    policy = ProductionPolicy(
        max_image_attempts=2,
        max_video_attempts=2,
        fallback="kenburns",
        skip_ready=True,
        auto_resume=True,
        technical_validation=True,
    )
    with jobs.project_job(
        project_dir.name,
        "Material engine acceptance",
        resource="comfy",
    ) as cancel_event:
        return run_unattended_materials(
            project_dir,
            project_dir.name,
            policy,
            cancel_event,
            generate,
            service_check=_comfy_available,
            dependency_wait_timeout=120.0,
        )


def reset_draft_videos(project_dir: Path) -> dict[str, Any]:
    """Archive acceptance drafts and make only those video operations pending."""

    project_dir = project_dir.expanduser().resolve()
    if not project_dir.name.startswith("engine-acceptance-"):
        raise ValueError("Refusing to reset media outside an acceptance project")
    cards = load_asset_board(project_dir)
    reset: list[dict[str, Any]] = []
    archive_dir = project_dir / "analysis"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for card in cards:
        if not card.ltx_draft or not str(card.video_output or "").strip():
            continue
        source = Path(card.video_output)
        if not source.is_file():
            continue
        archived = archive_dir / f"{source.stem}_draft{source.suffix}"
        shutil.copy2(source, archived)
        reset.append(
            {
                "shot_index": card.shot_index,
                "archived_draft": str(archived),
            }
        )
        card.video_output = ""
        card.assigned_media = ""
        card.ltx_draft = False
        card.status = "image_ready"
    if not reset:
        raise RuntimeError("No generated LTX draft found")
    write_asset_board(project_dir, cards)
    return {"project_dir": str(project_dir), "reset": reset}


def project_report(project_dir: Path) -> dict[str, Any]:
    project_dir = project_dir.expanduser().resolve()
    cards = load_asset_board(project_dir)
    run = load_material_run(project_dir)
    plan = load_material_plan(project_dir)
    media: list[dict[str, Any]] = []
    for card in cards:
        outputs = []
        for kind, raw in (
            ("image", card.image_output),
            ("video", card.video_output),
            ("assigned", card.assigned_media),
        ):
            if not str(raw or "").strip():
                continue
            path = Path(raw)
            outputs.append(
                {
                    "kind": kind,
                    "path": str(path),
                    "exists": path.is_file(),
                    "bytes": path.stat().st_size if path.is_file() else 0,
                }
            )
        media.append(
            {
                "card_id": card.card_id,
                "shot_index": card.shot_index,
                "visual_type": card.visual_type,
                "route": card.route,
                "status": card.status,
                "ready": card_is_ready(card.__dict__),
                "ltx_draft": card.ltx_draft,
                "outputs": outputs,
            }
        )
    return {
        "project_dir": str(project_dir),
        "preflight": preflight_material_plan(project_dir).to_dict(),
        "material_run": run,
        "frozen_plan_valid": plan is not None,
        "intentional_crash": (
            json.loads(
                (project_dir / ".acceptance_crash.json").read_text(encoding="utf-8")
            )
            if (project_dir / ".acceptance_crash.json").is_file()
            else None
        ),
        "all_cards_ready": bool(cards) and all(
            card_is_ready(card.__dict__) for card in cards
        ),
        "media": media,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("prepare", "run", "reset-drafts", "report"),
    )
    parser.add_argument("project_dir", type=Path)
    parser.add_argument(
        "--crash-after",
        type=int,
        default=0,
        help="Hard-exit after this many successful provider calls",
    )
    args = parser.parse_args()

    if args.action == "prepare":
        result = prepare_project(args.project_dir)
    elif args.action == "run":
        result = run_project(args.project_dir, crash_after=max(0, args.crash_after))
    elif args.action == "reset-drafts":
        result = reset_draft_videos(args.project_dir)
    else:
        result = project_report(args.project_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
