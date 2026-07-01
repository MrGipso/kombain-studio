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
    path = project.file_path("validation_report")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report