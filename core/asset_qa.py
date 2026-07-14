"""Typed observations and deterministic policy for generated-asset QA.

The vision model is deliberately not allowed to choose an action or emit a
free-form quality score.  It reports bounded observations.  This module then
maps technical evidence and those observations to a recommendation using a
versioned, testable policy.  ``retry_*`` remains a recommendation: this module
never invokes a generator.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from core import lm
from core.asset_qa_store import append_report
from core.media_observations import observe_image, observe_video
from core.models import timecode_to_seconds
from core.project import content_hash, is_within


QA_POLICY_VERSION = "asset-qa-policy-v1"
VISION_PROMPT_VERSION = "generated-frame-observations-v1"

QAMode = Literal["shadow", "review"]
QAAction = Literal["accept", "needs_review", "retry_image", "retry_video"]
FindingCode = Literal[
    "semantic_mismatch",
    "bad_anatomy",
    "broken_geometry",
    "garbled_text",
    "watermark",
    "identity_drift",
    "melted_or_duplicate_objects",
    "poor_composition",
    "other",
]


class VisionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: FindingCode
    severity: Literal["minor", "major"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=3, max_length=240)


class VisionObservations(BaseModel):
    """Only visible facts; there is intentionally no score/action/verdict."""

    model_config = ConfigDict(extra="forbid", strict=True)

    semantic_alignment: Literal["match", "partial", "mismatch", "uncertain"]
    composition: Literal["good", "usable", "poor", "uncertain"]
    expected_text_legible: Literal["yes", "no", "not_applicable", "uncertain"]
    findings: list[VisionFinding] = Field(max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=3, max_length=320)

class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: QAAction
    score: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str]
    summary: str


def _issue_codes(technical: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for issue in technical.get("issues", []) if isinstance(technical, dict) else []:
        if isinstance(issue, dict):
            code = str(issue.get("code", "technical_issue") or "technical_issue")
        else:
            code = str(issue or "technical_issue")
        if code not in result:
            result.append(code)
    return result


def decide_image_action(
    technical: dict[str, Any],
    observations: VisionObservations | None,
    *,
    threshold: float = 0.45,
    expected_text_required: bool = False,
) -> PolicyDecision:
    """Map image evidence to a deterministic recommendation."""
    if not 0.0 <= float(threshold) <= 1.0:
        raise ValueError("QA threshold must be between 0 and 1")
    if not bool(technical.get("ok", False)):
        reasons = _issue_codes(technical) or ["image_unreadable"]
        return PolicyDecision(
            action="retry_image",
            score=0.0,
            reason_codes=reasons,
            summary="Файл изображения отсутствует, пуст или не декодируется.",
        )
    if observations is None:
        return PolicyDecision(
            action="needs_review",
            score=0.0,
            reason_codes=["qa_unavailable"],
            summary="Vision-QA недоступна; требуется ручная проверка.",
        )

    penalty = 0.0
    reasons: list[str] = []
    semantic_penalty = {
        "match": 0.0,
        "partial": 0.25,
        "mismatch": 0.65,
        "uncertain": 0.20,
    }[observations.semantic_alignment]
    penalty += semantic_penalty
    if observations.semantic_alignment != "match":
        reasons.append(f"semantic_{observations.semantic_alignment}")

    composition_penalty = {
        "good": 0.0,
        "usable": 0.05,
        "poor": 0.20,
        "uncertain": 0.10,
    }[observations.composition]
    penalty += composition_penalty
    if observations.composition in {"poor", "uncertain"}:
        reasons.append(f"composition_{observations.composition}")

    if expected_text_required:
        text_penalty = {
            "yes": 0.0,
            "no": 0.30,
            "not_applicable": 0.10,
            "uncertain": 0.10,
        }[observations.expected_text_legible]
        penalty += text_penalty
        if observations.expected_text_legible != "yes":
            reasons.append(f"text_{observations.expected_text_legible}")

    retry_codes = {
        "semantic_mismatch",
        "bad_anatomy",
        "broken_geometry",
        "garbled_text",
        "watermark",
        "identity_drift",
        "melted_or_duplicate_objects",
    }
    confident_major = False
    # Grammar-constrained models can still repeat one categorical observation.
    # Collapse by code so repetition never amplifies a penalty or action.
    findings_by_code: dict[str, VisionFinding] = {}
    for finding in observations.findings:
        previous = findings_by_code.get(finding.code)
        if previous is None or (
            finding.severity == "major" and previous.severity == "minor"
        ) or (
            finding.severity == previous.severity
            and finding.confidence > previous.confidence
        ):
            findings_by_code[finding.code] = finding
    for finding in findings_by_code.values():
        penalty += 0.25 if finding.severity == "major" else 0.08
        if finding.code == "watermark":
            penalty += 0.25
        reasons.append(finding.code)
        if (
            finding.severity == "major"
            and finding.confidence >= 0.80
            and finding.code in retry_codes
        ):
            confident_major = True

    score = round(max(0.0, min(1.0, 1.0 - penalty)), 3)
    semantic_retry = (
        observations.semantic_alignment == "mismatch"
        and observations.confidence >= 0.75
    )
    if confident_major or semantic_retry:
        action: QAAction = "retry_image"
    elif (
        reasons
        or score < float(threshold)
        or observations.confidence < 0.55
    ):
        action = "needs_review"
        if observations.confidence < 0.55:
            reasons.append("low_observation_confidence")
        if score < float(threshold):
            reasons.append("below_policy_threshold")
    else:
        action = "accept"

    return PolicyDecision(
        action=action,
        score=score,
        reason_codes=list(dict.fromkeys(reasons)),
        summary=observations.summary,
    )


def decide_video_action(
    technical: dict[str, Any],
    *,
    dynamic: bool = True,
) -> PolicyDecision:
    """Deterministic policy for decode/duration/black/freeze observations."""
    if not bool(technical.get("ok", False)):
        reasons = _issue_codes(technical) or ["video_unreadable"]
        return PolicyDecision(
            action="retry_video",
            score=0.0,
            reason_codes=reasons,
            summary="Видеофайл не прошёл техническую проверку.",
        )

    black = max(0.0, min(1.0, float(technical.get("black_fraction", 0.0) or 0.0)))
    frozen = max(0.0, min(1.0, float(technical.get("frozen_fraction", 0.0) or 0.0)))
    reasons = _issue_codes(technical)
    retry_freeze = 0.65 if dynamic else 0.95
    review_freeze = 0.25 if dynamic else 0.80
    if black >= 0.80:
        action: QAAction = "retry_video"
        reasons.append("mostly_black")
    elif frozen >= retry_freeze:
        action = "retry_video"
        reasons.append("mostly_frozen")
    elif black >= 0.10 or frozen >= review_freeze or reasons:
        action = "needs_review"
        if black >= 0.10:
            reasons.append("black_segments")
        if frozen >= review_freeze:
            reasons.append("frozen_segments")
    else:
        action = "accept"
    score = round(max(0.0, 1.0 - max(black, frozen)), 3)
    summary = (
        f"Техническая QA видео: чёрные кадры {black:.1%}, "
        f"заморозка {frozen:.1%}."
    )
    return PolicyDecision(
        action=action,
        score=score,
        reason_codes=list(dict.fromkeys(reasons)),
        summary=summary,
    )


def _binding(path: Path, field: str) -> dict[str, Any]:
    return {
        "output_field": field,
        "filename": path.name,
        "sha256": content_hash(path),
        "size_bytes": path.stat().st_size,
    }


def _project_media_path(project_dir: Path, value: Any) -> tuple[Path, bool]:
    project_dir = Path(project_dir).resolve()
    raw = Path(str(value or ""))
    candidate = raw if raw.is_absolute() else project_dir / raw
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    return resolved, is_within(resolved, project_dir)


def _blocked_observation(path: Path) -> dict[str, Any]:
    return {
        "ok": False,
        "issues": [{
            "code": "path_outside_project",
            "message": "QA отказалась читать медиа вне каталога проекта",
            "details": {"filename": path.name},
        }],
    }


def _mode(value: Any) -> QAMode:
    mode = str(value or "shadow").strip().lower()
    return "review" if mode == "review" else "shadow"


def _vision_ttl(settings: Any) -> int | None:
    """Keep a QA batch warm briefly, then release its large VLM from VRAM."""
    auto = getattr(settings, "lm_auto_unload", True)
    if isinstance(auto, str):
        auto = auto.strip().lower() not in {"0", "false", "no", "off"}
    if not auto:
        return None
    try:
        delay = max(0, int(str(getattr(settings, "lm_unload_delay", "0") or "0")))
    except (TypeError, ValueError):
        delay = 60
    # Immediate unload between candidates thrashes a QA batch. Twenty seconds
    # covers adjacent cards while still returning 14+ GB to Comfy promptly.
    return delay if delay > 0 else 20


def _base_report(
    card: Any,
    *,
    kind: Literal["image", "video"],
    media: dict[str, Any],
    mode: QAMode,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "card_id": str(getattr(card, "card_id", "") or ""),
        "shot_index": int(getattr(card, "shot_index", 0) or 0),
        "kind": kind,
        "mode": mode,
        "policy_version": QA_POLICY_VERSION,
        "media": media,
        "recommendation": "needs_review",
        "score": 0.0,
        "reason_codes": [],
        "summary": "",
        "model": "",
        "transport": "",
        "attempts": 0,
        "technical": {},
        "vision": None,
        "error": "",
        "applied_action": "none",
    }


def _apply_decision(report: dict[str, Any], decision: PolicyDecision) -> None:
    report["recommendation"] = decision.action
    report["score"] = decision.score
    report["reason_codes"] = decision.reason_codes
    report["summary"] = decision.summary
    if report.get("mode") == "review" and decision.action != "accept":
        report["applied_action"] = "flagged_review"


def _cancel(cancel_check: Callable[[], Any] | None) -> None:
    if cancel_check is not None:
        cancel_check()


def inspect_image(
    project_dir: Path,
    card: Any,
    settings: Any,
    *,
    cancel_check: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Inspect and persist one image report; generation errors never originate here."""
    path, path_allowed = _project_media_path(
        project_dir, getattr(card, "image_output", "")
    )
    mode = _mode(getattr(settings, "ai_qa_mode", "shadow"))
    media = _binding(path, "image_output") if path_allowed and path.is_file() else {
        "output_field": "image_output",
        "filename": path.name,
        "sha256": content_hash(str(path)),
        "size_bytes": 0,
    }
    report = _base_report(card, kind="image", media=media, mode=mode)
    _cancel(cancel_check)
    technical = observe_image(path) if path_allowed else _blocked_observation(path)
    report["technical"] = technical
    if not technical.get("ok", False):
        _apply_decision(report, decide_image_action(technical, None))
        return append_report(project_dir, report)

    vision_model = str(getattr(settings, "lm_vision_model", "") or "").strip()
    report["model"] = vision_model
    if not vision_model:
        report["error"] = "Vision model is not configured"
        _apply_decision(report, decide_image_action(technical, None))
        return append_report(project_dir, report)

    expected_text = str(getattr(card, "screen_text", "") or "").strip()
    description = str(
        getattr(card, "description", "")
        or getattr(card, "image_prompt", "")
        or "generated frame"
    ).strip()
    prompt = (
        "Inspect the attached generated frame against the requested shot. Report only "
        "visible observations; never choose accept/retry/review and never invent a score.\n"
        f"Requested shot: {description[:1200]}\n"
        f"Expected on-screen text: {expected_text[:300] if expected_text else '[none]'}\n"
        "Use findings only for visible defects. A cinematic shallow depth of field is not "
        "a defect by itself. Evidence must name the visible region/object."
    )
    system = (
        "You are a strict visual inspection sensor. Return the supplied JSON schema only. "
        "Do not make workflow decisions."
    )
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    try:
        reply = lm.chat_structured(
            settings,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            VisionObservations,
            model=vision_model,
            image_bytes=path.read_bytes(),
            image_mime=mime,
            transport="openai",
            temperature=0.1,
            max_tokens=1200,
            timeout=180,
            ttl=_vision_ttl(settings),
            schema_name="generated_frame_observations",
        )
        _cancel(cancel_check)
        observations = reply.value
        report["vision"] = observations.model_dump(mode="json")
        report["transport"] = reply.transport
        report["attempts"] = reply.attempts
        raw_threshold = getattr(settings, "ai_qa_threshold", 0.45)
        threshold = 0.45 if raw_threshold in {None, ""} else float(raw_threshold)
        decision = decide_image_action(
            technical,
            observations,
            threshold=threshold,
            expected_text_required=bool(expected_text),
        )
        _apply_decision(report, decision)
    except Exception as exc:
        # Preserve cooperative cancellation raised after/during inference.  A
        # normal transport/schema failure reaches the safe unavailable report.
        _cancel(cancel_check)
        report["error"] = lm.explain_error(exc)
        _apply_decision(report, decide_image_action(technical, None))
    return append_report(project_dir, report)


def inspect_video(
    project_dir: Path,
    card: Any,
    settings: Any,
    *,
    cancel_check: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Run deterministic FFprobe/FFmpeg QA and persist one video report."""
    path, path_allowed = _project_media_path(
        project_dir, getattr(card, "video_output", "")
    )
    mode = _mode(getattr(settings, "ai_qa_mode", "shadow"))
    media = _binding(path, "video_output") if path_allowed and path.is_file() else {
        "output_field": "video_output",
        "filename": path.name,
        "sha256": content_hash(str(path)),
        "size_bytes": 0,
    }
    report = _base_report(card, kind="video", media=media, mode=mode)
    _cancel(cancel_check)
    expected_duration = max(
        0.0,
        float(timecode_to_seconds(getattr(card, "end", "")))
        - float(timecode_to_seconds(getattr(card, "start", ""))),
    )
    technical = (
        observe_video(
            path,
            str(getattr(settings, "ffmpeg", "") or "ffmpeg"),
            expected_duration=expected_duration or None,
        )
        if path_allowed
        else _blocked_observation(path)
    )
    _cancel(cancel_check)
    report["technical"] = technical
    route = str(getattr(card, "route", "") or "").lower()
    visual_type = str(getattr(card, "visual_type", "") or "").lower()
    dynamic = route not in {"kenburns", "stock", "reference"} and "still" not in visual_type
    _apply_decision(report, decide_video_action(technical, dynamic=dynamic))
    return append_report(project_dir, report)


def apply_review_mode(card: Any, reports: list[dict[str, Any]], *, log_note: str = "QA") -> None:
    """Apply only review-mode flags; never regenerate, approve, or clear media."""
    flagged = [
        report
        for report in reports
        if report.get("mode") == "review" and report.get("recommendation") != "accept"
    ]
    if not flagged:
        return
    card.status = "needs_review"
    for report in flagged:
        marker = f"[QA:{report.get('report_id', '')}]"
        if marker in str(getattr(card, "notes", "") or ""):
            continue
        reason = str(report.get("summary", "") or report.get("recommendation", "needs_review"))
        note = f"{log_note} {marker}: {reason}"
        card.notes = (card.notes + "\n" if card.notes else "") + note
