from __future__ import annotations

import json
import re
import statistics
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from core.models import timecode_to_seconds
from core.project import Project

WATERMARK_PATTERNS = [
    r"добавил[аи]?\s+субтитр",
    r"субтитры\s+.*(?:создал|добавил|редактор|автор)",
    r"dima\s*torzok",
    r"подписывайтесь",
    r"subscribe",
]


@dataclass
class ValidationIssue:
    level: str
    stage: str
    message: str
    item: str = ""
    details: dict | None = None


def _issue(level: str, stage: str, message: str, item: str = "", **details) -> ValidationIssue:
    return ValidationIssue(level=level, stage=stage, message=message, item=item, details=details or None)


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _watermark_hit(text: str) -> str | None:
    lowered = text.lower()
    for pattern in WATERMARK_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return pattern
    return None


def _duration(item: dict) -> int:
    return timecode_to_seconds(item.get("end", "0")) - timecode_to_seconds(item.get("start", "0"))


def _norm_prompt(text: str) -> str:
    text = re.sub(r"\b(?:alternate documentary composition|visual beat|subtle distinct camera movement)\b.*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text.lower()).strip()
    return text


def validate_transcript(project: Project) -> tuple[list[ValidationIssue], int | None]:
    issues: list[ValidationIssue] = []
    segments = _read_json(project.file_path("transcript_segments"))
    if not segments:
        return issues, None

    last_end = 0
    for index, segment in enumerate(segments, start=1):
        start = timecode_to_seconds(segment.get("start", "0"))
        end = timecode_to_seconds(segment.get("end", "0"))
        text = str(segment.get("text", "")).strip()
        if end <= start:
            issues.append(_issue("error", "transcript", "Сегмент имеет неверную длительность", f"segment {index}", start=segment.get("start"), end=segment.get("end")))
        if start < last_end - 1:
            issues.append(_issue("warning", "transcript", "Таймкоды сегментов идут не по порядку", f"segment {index}", previous_end=last_end, start=start))
        hit = _watermark_hit(text)
        if hit:
            level = "error" if index == len(segments) else "warning"
            issues.append(_issue(level, "transcript", "Сегмент похож на watermark/мусор субтитров", f"segment {index}", text=text, pattern=hit))
        if index == len(segments) and _duration(segment) >= 15 and len(text.split()) <= 6:
            issues.append(_issue("warning", "transcript", "Последний сегмент подозрительно длинный и короткий по тексту", f"segment {index}", text=text, duration=_duration(segment)))
        last_end = max(last_end, end)

    return issues, last_end


def validate_scenes(project: Project, transcript_end: int | None) -> tuple[list[ValidationIssue], dict[int, dict]]:
    issues: list[ValidationIssue] = []
    scenes = _read_json(project.file_path("scenes"))
    if not scenes:
        return issues, {}

    scene_by_index: dict[int, dict] = {}
    previous_end = 0
    for scene in scenes:
        index = int(scene.get("index", 0) or 0)
        scene_by_index[index] = scene
        start = timecode_to_seconds(scene.get("start", "0"))
        end = timecode_to_seconds(scene.get("end", "0"))
        title = str(scene.get("title", f"scene {index}"))
        voice_text = str(scene.get("voice_text", ""))
        if end <= start:
            issues.append(_issue("error", "scenes", "Сцена имеет неверную длительность", title, start=scene.get("start"), end=scene.get("end")))
        if start < previous_end - 1:
            issues.append(_issue("warning", "scenes", "Сцены пересекаются или идут не по порядку", title, previous_end=previous_end, start=start))
        if end - start > 90:
            issues.append(_issue("warning", "scenes", "Сцена длиннее 90 секунд", title, duration=end - start))
        hit = _watermark_hit(voice_text)
        if hit:
            issues.append(_issue("error", "scenes", "voice_text содержит watermark/мусор субтитров", title, pattern=hit))
        if transcript_end is not None and end > transcript_end + 2:
            issues.append(_issue("warning", "scenes", "Сцена выходит за конец транскрипции", title, scene_end=end, transcript_end=transcript_end))
        previous_end = max(previous_end, end)

    return issues, scene_by_index


def validate_shots(project: Project, scene_by_index: dict[int, dict], transcript_end: int | None) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    shots = _read_json(project.file_path("shot_list"))
    if not shots:
        return issues

    total_duration = 0
    previous_prompt = ""
    duplicate_run: list[int] = []

    for shot in shots:
        index = int(shot.get("index", 0) or 0)
        scene_index = int(shot.get("scene_index", 0) or 0)
        start = timecode_to_seconds(shot.get("start", "0"))
        end = timecode_to_seconds(shot.get("end", "0"))
        duration = max(0, end - start)
        total_duration += duration
        description = str(shot.get("description", ""))
        image_prompt = str(shot.get("image_prompt", ""))

        if end <= start:
            issues.append(_issue("error", "shots", "Кадр имеет неверную длительность", f"shot {index}", start=shot.get("start"), end=shot.get("end")))
        scene = scene_by_index.get(scene_index)
        if scene:
            scene_start = timecode_to_seconds(scene.get("start", "0"))
            scene_end = timecode_to_seconds(scene.get("end", "0"))
            if start < scene_start - 1 or end > scene_end + 1:
                issues.append(_issue("error", "shots", "Кадр выходит за границы своей сцены", f"shot {index}", scene=scene_index, shot_start=start, shot_end=end, scene_start=scene_start, scene_end=scene_end))
        else:
            issues.append(_issue("error", "shots", "Кадр ссылается на несуществующую сцену", f"shot {index}", scene=scene_index))

        match = re.search(r"вариант\s+\d+/(\d+)", description, flags=re.IGNORECASE)
        if match and int(match.group(1)) >= 8:
            issues.append(_issue("warning", "shots", "Кадр выглядит как размноженный вариант одного длинного шота", f"shot {index}", description=description))

        norm_prompt = _norm_prompt(image_prompt)
        similar = bool(previous_prompt) and SequenceMatcher(None, previous_prompt, norm_prompt).ratio() >= 0.96
        if similar:
            duplicate_run.append(index)
        else:
            if len(duplicate_run) >= 3:
                issues.append(_issue("warning", "shots", "Несколько подряд кадров имеют почти одинаковый image_prompt", f"shots {duplicate_run[0]-1}-{duplicate_run[-1]}", shots=[duplicate_run[0] - 1, *duplicate_run]))
            duplicate_run = []
        previous_prompt = norm_prompt

    if len(duplicate_run) >= 3:
        issues.append(_issue("warning", "shots", "Несколько подряд кадров имеют почти одинаковый image_prompt", f"shots {duplicate_run[0]-1}-{duplicate_run[-1]}", shots=[duplicate_run[0] - 1, *duplicate_run]))

    if transcript_end and total_duration > transcript_end * 1.25:
        issues.append(_issue("warning", "shots", "Суммарная длительность кадров заметно больше транскрипции", "shot_list", shot_duration=total_duration, transcript_duration=transcript_end))

    return issues


def _card_route(card: dict) -> str:
    route = str(card.get("route", "") or "").lower()
    vt = str(card.get("visual_type", "") or "").lower()
    if route in {"stock", "kenburns", "image", "image+video"}:
        return route
    if vt == "stock_video":
        return "stock"
    if vt == "still_kenburns":
        return "kenburns"
    if vt == "ai_image":
        return "image"
    return "image+video" if card.get("video_model") else "image"


def _expected_visual_type(route: str) -> str:
    return {
        "stock": "stock_video",
        "kenburns": "still_kenburns",
        "image": "ai_image",
        "image+video": "ai_image_then_i2v",
    }.get(route, "")


def _path_exists(value: str) -> bool:
    return bool(value) and Path(value).exists()


def validate_asset_board(project: Project) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    cards = _read_json(project.file_path("asset_board"))
    if not cards:
        return issues

    for raw in cards:
        card = raw if isinstance(raw, dict) else {}
        shot = card.get("shot_index", "?")
        item = f"card {shot}"
        route = _card_route(card)
        visual_type = str(card.get("visual_type", "") or "").lower()
        expected_vt = _expected_visual_type(route)
        status = str(card.get("status", "") or "").lower()
        excluded = status in {"skipped", "rejected"}
        assigned_media = str(card.get("assigned_media", "") or "")
        image_output = str(card.get("image_output", "") or "")
        video_output = str(card.get("video_output", "") or "")

        if expected_vt and visual_type != expected_vt:
            issues.append(_issue(
                "warning", "asset_board", "route не совпадает с visual_type", item,
                route=route, visual_type=visual_type, expected_visual_type=expected_vt,
            ))

        if assigned_media and not _path_exists(assigned_media):
            issues.append(_issue("error", "asset_board", "assigned_media указывает на несуществующий файл", item, path=assigned_media))
        if image_output and not _path_exists(image_output):
            issues.append(_issue("error", "asset_board", "image_output указывает на несуществующий файл", item, path=image_output))
        if video_output and not _path_exists(video_output):
            issues.append(_issue("error", "asset_board", "video_output указывает на несуществующий файл", item, path=video_output))

        if not excluded and not assigned_media:
            if route in {"stock", "kenburns", "image+video"} and not video_output:
                level = "error" if status == "generated" else "warning"
                issues.append(_issue(level, "asset_board", "Маршрут требует video_output, но видео отсутствует", item, route=route, status=status))
            if route == "image" and not image_output:
                level = "error" if status == "generated" else "warning"
                issues.append(_issue(level, "asset_board", "Маршрут image требует image_output, но картинка отсутствует", item, route=route, status=status))

        if route == "image+video" and image_output and not video_output and not excluded:
            issues.append(_issue("warning", "asset_board", "Есть только картинка для AI-video кадра — экспорт не будет считать кадр готовым", item))

        if route == "stock" and status == "generated":
            if not str(card.get("source_url", "") or "").strip():
                issues.append(_issue("warning", "asset_board", "Сток-кадр сгенерирован без source_url", item))
            if not str(card.get("source_license", "") or "").strip():
                issues.append(_issue("warning", "asset_board", "Сток-кадр сгенерирован без зафиксированной лицензии", item))
            try:
                score = float(card.get("stock_score", 0) or 0)
            except (TypeError, ValueError):
                score = 0.0
            if score <= 0:
                issues.append(_issue("warning", "asset_board", "Сток-кадр сгенерирован без stock_score", item))

        if card.get("text_overlay_enabled"):
            overlay_text = str(card.get("text_overlay_text", "") or card.get("screen_text", "") or "").strip()
            if not overlay_text:
                issues.append(_issue("warning", "asset_board", "Overlay включён, но текст пустой", item))

    return issues


def summarize(issues: Iterable[ValidationIssue]) -> dict:
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        counts[issue.level] = counts.get(issue.level, 0) + 1
    status = "error" if counts.get("error", 0) else "warning" if counts.get("warning", 0) else "ok"
    return {"status": status, "counts": counts}


def scene_metrics(project: Project) -> dict:
    """Quality metrics for the scene segmentation, so the report shows HOW the cut
    went (count, length spread, outliers) rather than just timing errors."""
    scenes = _read_json(project.file_path("scenes")) or []
    if not scenes:
        return {"count": 0}
    durs = [max(0, _duration(s)) for s in scenes]
    words = [len(str(s.get("voice_text", "")).split()) for s in scenes]
    seg_min = int(project.metadata.get("seg_min_seconds", 20) or 20)
    seg_max = int(project.metadata.get("seg_max_seconds", 90) or 90)
    avg = statistics.fmean(durs) if durs else 0.0
    std = statistics.pstdev(durs) if len(durs) > 1 else 0.0
    return {
        "count": len(scenes),
        "duration_min": min(durs) if durs else 0,
        "duration_avg": round(avg, 1),
        "duration_max": max(durs) if durs else 0,
        "duration_stddev": round(std, 1),
        "evenness": round(std / avg, 2) if avg else 0,  # 0 = равномерно, >1 = очень рвано
        "under_min": sum(1 for d in durs if d < seg_min),
        "over_max": sum(1 for d in durs if d > seg_max),
        "words_avg": round(statistics.fmean(words), 1) if words else 0,
    }


def validate_project(project: Project) -> dict:
    issues: list[ValidationIssue] = []
    transcript_issues, transcript_end = validate_transcript(project)
    issues.extend(transcript_issues)
    scene_issues, scene_by_index = validate_scenes(project, transcript_end)
    issues.extend(scene_issues)
    issues.extend(validate_shots(project, scene_by_index, transcript_end))
    issues.extend(validate_asset_board(project))

    payload = {
        "project": project.name,
        "summary": summarize(issues),
        "segmentation": scene_metrics(project),
        "issues": [asdict(issue) for issue in issues],
    }
    return payload


def write_validation_report(project: Project) -> dict:
    if not project.dir.exists():
        raise FileNotFoundError(f"Project directory does not exist: {project.dir}")
    report = validate_project(project)
    project.write_json("validation_report", report)
    upstream = project.stage_states()
    # A report over stale planning artifacts is still useful to the user, but it
    # must not turn the DAG green. Master export will surface the stale stage and
    # require rebuilding it first.
    if project.stage_dependencies_current("validation"):
        project.mark_stage_completed(
            "validation",
            report,
            input_hash=project.content_hash({
                stage: {
                    "hash": state.get("hash"),
                    "revision": state.get("revision", 0),
                }
                for stage, state in upstream.items()
                if stage in ("transcript", "scenes", "shots", "assets", "timeline")
            }),
        )
    else:
        project.invalidate_stage(
            "validation",
            reason="validation dependencies are not current",
        )
    return report
