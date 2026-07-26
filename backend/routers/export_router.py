from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from core.config import load_settings
from core.export_profiles import (
    ExportProfileName,
    export_profile_catalog,
    resolve_export_profile,
)
from core.project import content_hash, is_within, open_project, safe_project_path
from core.export import export_video, card_route as core_card_route, VIDEO_ROUTES
from core.export_manifest import (
    MASTER_MANIFEST_TYPE,
    build_edit_contract,
    evaluate_master_provenance,
    load_master_export_index,
    write_master_export_manifest,
)
from core.media_qc import check_final_media
from core.validation import validate_project, write_validation_report

from backend.services.sse_manager import SSEManager
from backend.services import jobs, runlog
from backend.models.schemas import (
    ExportGateReason,
    ExportGateReport,
    ExportGateSummary,
    ExportMode,
    MasterManifestInfo,
)

router = APIRouter()
sse = SSEManager()

EXCLUDED_STATUSES = {"skipped", "rejected"}
EXPORT_SETTING_KEYS = (
    "export_loudnorm",
    "music_normalize",
    "export_unify_grade",
    "export_grain",
    "sfx_enabled",
    "sfx_volume_db",
    "ltx_resolution",
)


class _HandledExportFailure(RuntimeError):
    """Failure already persisted and emitted to SSE; still fail the durable job."""


def _export_settings_contract(project) -> dict[str, Any]:
    try:
        effective = project.effective_settings()
    except (AttributeError, TypeError):
        effective = vars(project.settings) if hasattr(project.settings, "__dict__") else {}
    return {key: effective.get(key) for key in EXPORT_SETTING_KEYS}


def _timeline_payload(project, cards: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "settings": project.read_json("timeline") or {"voice_start": "00:00:00"},
        "cards": cards,
        "music": {
            key: project.metadata.get(key)
            for key in ("music_enabled", "music_volume_db", "music_duck", "music_selected")
        },
    }


def _master_input_snapshot(
    project,
    cards: list[dict[str, Any]],
    provenance: dict[str, Any],
    profile: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    """Hash every Master decision/input so preflight cannot become stale."""
    settings = _export_settings_contract(project)
    timeline = project.read_json("timeline") or {"voice_start": "00:00:00"}
    edit_contract = build_edit_contract(
        project.dir,
        cards,
        timeline_settings=timeline if isinstance(timeline, dict) else {},
        project_metadata=project.metadata,
        settings_contract=settings,
    )
    required_states = {
        stage: state
        for stage, state in project.stage_states().items()
        if stage in ("transcript", "scenes", "shots", "assets", "timeline")
    }
    fingerprint = project.content_hash({
        "cards": cards,
        "profile": profile,
        "settings": settings,
        "edit_contract": edit_contract,
        "stage_states": required_states,
        "variant_manifest": provenance.get("variant_manifest"),
    })
    return fingerprint, settings, edit_contract, required_states


def _validate_profile_for_mode(mode: ExportMode, profile_name: str) -> None:
    if mode == "master" and profile_name == "preview":
        raise HTTPException(422, "Master export cannot use the preview profile")


@router.get("/{project_id:str}/export-profiles")
async def get_export_profiles(project_id: str):
    """Return the server-owned export contracts rendered by the web client."""
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    if not path.is_dir():
        raise HTTPException(404, "Project not found")
    return export_profile_catalog()


def _card_route(card: dict[str, Any]) -> str:
    """Delegate to the single route resolver in core.export so the Master gate
    and the exporter never disagree (e.g. on the `reference` route)."""
    return core_card_route(card)


def _usable_card_media(card: dict[str, Any]) -> str:
    """Return the exact file the current exporter can consume, or an empty string."""
    assigned = str(card.get("assigned_media", "") or "").strip()
    if assigned and Path(assigned).is_file():
        return assigned

    route = _card_route(card)
    field = "video_output" if route in VIDEO_ROUTES else "image_output"
    candidate = str(card.get(field, "") or "").strip()
    return candidate if candidate and Path(candidate).is_file() else ""


def evaluate_export_gate(
    cards: list[dict[str, Any]],
    validation_report: dict[str, Any] | None,
    mode: ExportMode = "master",
    stage_states: dict[str, dict[str, Any]] | None = None,
    provenance_report: dict[str, Any] | None = None,
) -> ExportGateReport:
    """Pure Master-export policy evaluation (filesystem reads only for media paths).

    Draft deliberately keeps the historical permissive behaviour. Master treats
    every non-excluded card as required and must pass both project validation and
    per-card editorial/finality checks.
    """
    total = len(cards)
    excluded = sum(
        1 for card in cards
        if str(card.get("status", "") or "").strip().lower() in EXCLUDED_STATUSES
    )
    required = total - excluded

    if mode == "draft":
        return ExportGateReport(
            mode=mode,
            allowed=True,
            summary=ExportGateSummary(
                cards_total=total,
                cards_required=required,
                cards_ready=sum(
                    1 for card in cards
                    if str(card.get("status", "") or "").strip().lower() not in EXCLUDED_STATUSES
                    and _usable_card_media(card)
                ),
                cards_excluded=excluded,
            ),
        )

    reasons: list[ExportGateReason] = []
    for stage in ("transcript", "scenes", "shots", "assets", "timeline"):
        state = (stage_states or {}).get(stage)
        if not state:
            continue
        status = str(state.get("status", "pending"))
        if status != "completed":
            reasons.append(ExportGateReason(
                code="pipeline_not_current",
                message=f"Этап {stage} имеет статус {status}; обновите pipeline перед Master",
                item=stage,
                details={"status": status, "invalidated_by": state.get("invalidated_by")},
            ))
    report = validation_report if isinstance(validation_report, dict) else {}
    issues = report.get("issues", [])
    if not isinstance(issues, list):
        issues = []
    validation_errors = [
        issue for issue in issues
        if isinstance(issue, dict) and str(issue.get("level", "")).lower() == "error"
    ]
    # Some callers may only have a summary. Preserve the gate even if detailed
    # issues are absent or an older validation-report shape is loaded.
    try:
        summary_error_count = int(report.get("summary", {}).get("counts", {}).get("error", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        summary_error_count = 0

    for issue in validation_errors:
        issue_message = str(issue.get("message", "Validation error") or "Validation error")
        stage = str(issue.get("stage", "validation") or "validation")
        reasons.append(ExportGateReason(
            code="validation_error",
            message=f"{stage}: {issue_message}",
            item=str(issue.get("item", "") or ""),
            details=issue.get("details") if isinstance(issue.get("details"), dict) else None,
        ))
    for _ in range(max(0, summary_error_count - len(validation_errors))):
        reasons.append(ExportGateReason(
            code="validation_error",
            message="В проверке проекта осталась неуточнённая ошибка",
        ))

    if required == 0:
        reasons.append(ExportGateReason(
            code="no_required_cards",
            message="Нет кадров для Master-экспорта",
        ))

    ready = 0
    for card_index, card in enumerate(cards):
        status = str(card.get("status", "") or "").strip().lower()
        if status in EXCLUDED_STATUSES:
            continue

        shot_value = card.get("shot_index")
        try:
            shot_index = int(shot_value) if shot_value is not None else None
        except (TypeError, ValueError):
            shot_index = None
        label = f"Shot {shot_index}" if shot_index is not None else f"Card {card_index + 1}"
        media = _usable_card_media(card)
        if media:
            ready += 1
        else:
            reasons.append(ExportGateReason(
                code="missing_media",
                message=f"{label}: нет доступного медиафайла",
                card_index=card_index,
                shot_index=shot_index,
            ))

        if status == "needs_review":
            reasons.append(ExportGateReason(
                code="needs_review",
                message=f"{label}: кадр всё ещё требует проверки",
                card_index=card_index,
                shot_index=shot_index,
            ))
        if bool(card.get("ltx_draft", False)):
            reasons.append(ExportGateReason(
                code="ltx_draft",
                message=f"{label}: LTX-черновик нужно перерендерить в финальном качестве",
                card_index=card_index,
                shot_index=shot_index,
            ))
        # The `approved` flag is dead vocabulary from the removed approve-gate
        # workflow: the board replaced it with include/exclude, nothing sets it
        # anymore, and generation resets it on every run. Reporting it produced
        # one unclearable finding per card and drowned the real ones.

    if provenance_report is not None:
        raw_reasons = provenance_report.get("reasons", [])
        if isinstance(raw_reasons, list):
            for reason in raw_reasons:
                if not isinstance(reason, dict):
                    continue
                try:
                    reasons.append(ExportGateReason.model_validate(reason))
                except Exception:
                    reasons.append(ExportGateReason(
                        code="invalid_provenance_report",
                        message="Проверка provenance вернула некорректный результат",
                    ))

    return ExportGateReport(
        mode=mode,
        allowed=not reasons,
        summary=ExportGateSummary(
            blockers=len(reasons),
            validation_errors=max(len(validation_errors), summary_error_count),
            cards_total=total,
            cards_required=required,
            cards_ready=ready,
            cards_excluded=excluded,
        ),
        reasons=reasons,
    )


@router.post("/{project_id:str}/validate")
async def validate(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    try:
        with jobs.project_mutation(project.dir.name, "Project validation"):
            current = open_project(path, load_settings()) or project
            report = write_validation_report(current)
        return report
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/{project_id:str}/export")
async def export(
    project_id: str,
    mode: Annotated[
        ExportMode,
        Query(description="Draft preserves permissive export; Master enforces the quality gate."),
    ] = "draft",
    profile: Annotated[
        ExportProfileName,
        Query(description="Named, validated FFmpeg delivery contract."),
    ] = "delivery",
):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")

    selected_profile = resolve_export_profile(profile)
    _validate_profile_for_mode(mode, selected_profile.name)
    cards: list[dict[str, Any]] = []
    preflight_fingerprint = ""
    try:
        # Initial preflight itself must be a coherent read. It releases this
        # short mutation lock before the SSE worker starts; the full fingerprint
        # is then compared again under the durable export job lock.
        with jobs.project_mutation(project.dir.name, "Export preflight"):
            project = open_project(path, load_settings()) or project
            raw_cards = project.read_json("asset_board")
            cards = (
                [card for card in raw_cards if isinstance(card, dict)]
                if isinstance(raw_cards, list)
                else []
            )
            validation_report = validate_project(project) if mode == "master" else None
            provenance_report = (
                evaluate_master_provenance(project.dir, cards)
                if mode == "master"
                else None
            )
            stage_states = project.stage_states() if mode == "master" else None
            if mode == "master":
                (
                    preflight_fingerprint,
                    _,
                    _,
                    stage_states,
                ) = _master_input_snapshot(
                    project,
                    cards,
                    provenance_report or {},
                    selected_profile.as_public_dict(),
                )
            gate = evaluate_export_gate(
                cards,
                validation_report,
                mode,
                stage_states=stage_states,
                provenance_report=provenance_report,
            )
    except jobs.ProjectBusy as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Export preflight failed: {exc}")
    gate_payload = gate.model_dump(mode="json")

    async def event_stream():
        sub_id, q = sse.subscribe(project.dir.name)
        loop = asyncio.get_event_loop()

        # The client always receives a typed preflight result. It is ADVISORY:
        # the operator of a local, single-user tool is the reviewer, and
        # refusing to render forced them to debug the gate instead of watching
        # the video. Findings are reported and logged, never enforced — a shot
        # without media simply does not ship, exactly as the exporter already
        # handles it.
        sse.emit_to(sub_id, "gate", gate_payload)
        if not gate.allowed:
            sse.emit_to(sub_id, "log", {
                "message": (
                    f"Предупреждение: {gate.summary.blockers} замечаний по материалам — "
                    "сборка продолжается, подробности в панели готовности."
                ),
            })

        def log_handler(msg: str):
            loop.call_soon_threadsafe(sse.emit_to, sub_id, "log", {"message": msg})

        def run_sync():
            try:
                job_label = (
                    f"{'Master' if mode == 'master' else 'Draft'} export · "
                    f"{selected_profile.label}"
                )
                profile_suffix = (
                    "" if selected_profile.name == "delivery" else f"_{selected_profile.name}"
                )
                with jobs.project_job(project.dir.name, job_label, resource="ffmpeg") as cancel_event:
                    active_project = open_project(project.dir, load_settings()) or project
                    current_raw = active_project.read_json("asset_board")
                    cards_for_export = (
                        [card for card in current_raw if isinstance(card, dict)]
                        if isinstance(current_raw, list)
                        else []
                    )
                    ffmpeg = active_project.settings.ffmpeg or "ffmpeg"
                    export_settings = _export_settings_contract(active_project)
                    edit_contract: dict[str, Any] = {}
                    provenance_for_export = provenance_report
                    completion_gate_payload = gate_payload
                    if mode == "master":
                        # The browser-facing gate is advisory until the durable
                        # project lock is held. Recompute the complete gate and
                        # every render input inside that lock, then require the
                        # snapshot to match the one the user approved.
                        current_validation = write_validation_report(active_project)
                        current_provenance = evaluate_master_provenance(
                            active_project.dir, cards_for_export
                        )
                        (
                            locked_fingerprint,
                            export_settings,
                            edit_contract,
                            locked_stage_states,
                        ) = _master_input_snapshot(
                            active_project,
                            cards_for_export,
                            current_provenance,
                            selected_profile.as_public_dict(),
                        )
                        if locked_fingerprint != preflight_fingerprint:
                            current_provenance = dict(current_provenance)
                            current_provenance["ok"] = False
                            current_provenance["reasons"] = [
                                *list(current_provenance.get("reasons", [])),
                                {
                                    "code": "export_inputs_changed",
                                    "message": (
                                        "Входы Master изменились после preflight; "
                                        "запустите Master повторно"
                                    ),
                                    "item": "master-input-contract",
                                },
                            ]
                        locked_gate = evaluate_export_gate(
                            cards_for_export,
                            current_validation,
                            "master",
                            stage_states=locked_stage_states,
                            provenance_report=current_provenance,
                        )
                        locked_payload = locked_gate.model_dump(mode="json")
                        completion_gate_payload = locked_payload
                        if not locked_gate.allowed:
                            # Advisory, like the preflight: re-emit the refreshed
                            # findings so the panel stays truthful, then render.
                            loop.call_soon_threadsafe(
                                sse.emit_to, sub_id, "gate", locked_payload
                            )
                        provenance_for_export = current_provenance

                    jobs.check(cancel_event)
                    # ffmpeg logs a line per segment/frame -> each line is a
                    # cancel checkpoint; the job stops after the current segment.
                    result = export_video(
                        project_dir=active_project.dir,
                        ffmpeg_path=ffmpeg,
                        log_callback=jobs.checking_logger(
                            runlog.tee(active_project.dir, job_label, log_handler),
                            cancel_event,
                        ),
                        output_name=(
                            f"{mode}{profile_suffix}_"
                            f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_"
                            f"{uuid.uuid4().hex[:12]}.mp4"
                        ),
                        profile=selected_profile,
                        runtime_settings=active_project.settings,
                        # Always fill in missing sound when the layer is on.
                        # Master used to refuse to generate, so a delivery could
                        # silently ship scenes without their ambience whenever a
                        # clip had not been generated by hand beforehand. The
                        # SFX layer is optional and already degrades to "no SFX"
                        # on any failure, so generating is never a blocker.
                        generate_missing_sfx=True,
                    )
                    if not result:
                        active_project.mark_stage_failed("export", "Export failed")
                        loop.call_soon_threadsafe(sse.emit_to, sub_id, "error", {
                            "code": "export_failed",
                            "message": "Export failed",
                            "profile": selected_profile.as_public_dict(),
                        })
                        raise _HandledExportFailure("Export failed")

                    jobs.check(cancel_event)
                    technical_qc = check_final_media(
                        result,
                        ffmpeg=ffmpeg,
                        require_audio=(mode == "master"),
                        profile=selected_profile,
                    )
                    if not technical_qc["ok"]:
                        # The file already exists at this point, so discarding
                        # the render helps nobody: report the findings and let
                        # the operator judge whether to keep or rebuild it.
                        loop.call_soon_threadsafe(sse.emit_to, sub_id, "log", {
                            "message": (
                                "Технический контроль файла: "
                                f"ошибок {technical_qc['counts']['error']}, "
                                f"предупреждений {technical_qc['counts']['warning']} — "
                                "файл собран, проверьте отчёт ниже."
                            ),
                        })
                    cards_after = active_project.read_json("asset_board") or []
                    # The audit manifest certifies WHICH inputs produced this
                    # file. If the board changed mid-render, that claim would be
                    # false — so the manifest is skipped rather than made to
                    # lie. The video itself is still delivered: a manifest is
                    # provenance, not a licence to render.
                    skip_manifest_reason = ""
                    if mode == "master":
                        post_provenance = evaluate_master_provenance(
                            active_project.dir, cards_after
                        )
                        post_fingerprint, _, post_edit_contract, _ = _master_input_snapshot(
                            active_project,
                            cards_after,
                            post_provenance,
                            selected_profile.as_public_dict(),
                        )
                        if (
                            post_fingerprint != locked_fingerprint
                            or not post_provenance.get("ok")
                        ):
                            skip_manifest_reason = (
                                "материалы изменились во время сборки — "
                                "видео готово, но манифест не выписан"
                            )
                            loop.call_soon_threadsafe(sse.emit_to, sub_id, "log", {
                                "message": f"Предупреждение: {skip_manifest_reason}.",
                            })
                        else:
                            provenance_for_export = post_provenance
                            edit_contract = post_edit_contract
                    if active_project.content_hash(cards_after) != active_project.content_hash(cards_for_export):
                        active_project.mark_stage_completed(
                            "assets",
                            cards_after,
                            input_hash=active_project.content_hash(
                                active_project.read_json("shot_list") or []
                            ),
                            metadata={"updated_during_export": True},
                        )
                    timeline_payload = _timeline_payload(active_project, cards_after)
                    active_project.mark_stage_completed(
                        "timeline",
                        timeline_payload,
                        input_hash=active_project.content_hash(cards_after),
                    )
                    if mode == "master":
                        write_validation_report(active_project)
                    jobs.check(cancel_event)
                    master_manifest = None
                    if mode == "master" and not skip_manifest_reason:
                        master_manifest = write_master_export_manifest(
                            result,
                            project_id=active_project.dir.name,
                            project_name=active_project.name,
                            project_schema_version=active_project.schema_version,
                            provenance=provenance_for_export or {},
                            profile=selected_profile.as_public_dict(),
                            settings_contract=export_settings,
                            technical_qc=technical_qc,
                            edit_contract=edit_contract,
                            ffmpeg=ffmpeg,
                        )
                    else:
                        # Master sidecar publication is the commit point; after
                        # it succeeds, cancellation must not leave an orphaned
                        # immutable delivery pair without stage metadata.
                        jobs.check(cancel_event)
                    active_project.mark_stage_completed(
                        "export",
                        result,
                        input_hash=active_project.content_hash({
                            "mode": mode,
                            "profile": selected_profile.as_public_dict(),
                            "timeline": timeline_payload,
                            "settings": export_settings,
                            "edit_contract": edit_contract,
                        }),
                        metadata={
                            "mode": mode,
                            "profile": selected_profile.as_public_dict(),
                            "path": str(result),
                            "technical_qc": technical_qc,
                            "master_manifest": master_manifest,
                        },
                    )
                    loop.call_soon_threadsafe(sse.emit_to, sub_id, "done", {
                        "path": str(result),
                        "mode": mode,
                        "profile": selected_profile.as_public_dict(),
                        "gate": completion_gate_payload,
                        "technical_qc": technical_qc,
                        "master_manifest": master_manifest,
                    })
            except _HandledExportFailure:
                # The durable context observes the exception and records a
                # failed job before it is handled here; SSE/stage details were
                # emitted at the precise failure site above.
                pass
            except (jobs.ProjectBusy, jobs.JobCancelled) as exc:
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "error", {
                    "code": "export_cancelled" if isinstance(exc, jobs.JobCancelled) else "project_busy",
                    "message": str(exc),
                    "profile": selected_profile.as_public_dict(),
                })
            except Exception as exc:
                try:
                    with jobs.project_mutation(project.dir.name, "Record export failure"):
                        failed_project = open_project(project.dir, load_settings()) or project
                        failed_project.mark_stage_failed("export", str(exc))
                except Exception:
                    pass
                loop.call_soon_threadsafe(sse.emit_to, sub_id, "error", {
                    "code": "export_failed",
                    "message": str(exc),
                    "profile": selected_profile.as_public_dict(),
                })

        loop.run_in_executor(None, run_sync)

        async for msg in sse.generator(sub_id):
            yield msg

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _valid_master_deliveries(exports_dir: Path) -> list[tuple[Path, Path, dict[str, Any]]]:
    """Return verified ``(sidecar, mp4, payload)`` records, newest first."""
    if not exports_dir.is_dir():
        return []
    try:
        index_records = load_master_export_index(exports_dir)
    except Exception:
        return []
    anchors = {
        str(record.get("sidecar", "")): record
        for record in index_records
        if isinstance(record, dict)
    }
    if not anchors:
        return []
    anchor_order = {
        str(record.get("sidecar", "")): index
        for index, record in enumerate(index_records)
        if isinstance(record, dict)
    }
    candidates: list[Path] = []
    for candidate in exports_dir.glob("*.mp4.manifest.json"):
        try:
            if candidate.is_file() and is_within(candidate, exports_dir):
                candidates.append(candidate)
        except OSError:
            continue
    candidates.sort(key=lambda item: anchor_order.get(item.name, -1), reverse=True)

    valid: list[tuple[Path, Path, dict[str, Any]]] = []
    for sidecar in candidates:
        try:
            anchor = anchors.get(sidecar.name)
            if (
                not anchor
                or sidecar.stat().st_size != int(anchor.get("sidecar_size", -1))
                or content_hash(sidecar) != str(anchor.get("sidecar_sha256", "") or "")
            ):
                continue
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            export_payload = payload.get("export", {}) if isinstance(payload, dict) else {}
            output_name = str(export_payload.get("path", "") or "").strip()
            if (
                not isinstance(payload, dict)
                or payload.get("manifest_type") != MASTER_MANIFEST_TYPE
                or not output_name
                or Path(output_name).name != output_name
                or sidecar.name != f"{output_name}.manifest.json"
                or str(payload.get("manifest_id", "")) != str(anchor.get("manifest_id", ""))
                or output_name != str(anchor.get("output", ""))
            ):
                continue
            output = (exports_dir / output_name).resolve()
            if not is_within(output, exports_dir) or not output.is_file():
                continue
            expected_hash = str(export_payload.get("sha256", "") or "").lower()
            expected_size = int(export_payload.get("size", -1))
            if (
                output.stat().st_size != expected_size
                or expected_size != int(anchor.get("output_size", -1))
                or expected_hash != str(anchor.get("output_sha256", "") or "")
                or content_hash(output) != expected_hash
            ):
                continue
            valid.append((sidecar.resolve(), output, payload))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return valid


@router.get("/{project_id:str}/download-manifest")
async def download_manifest(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    deliveries = _valid_master_deliveries(path / "exports")
    if not deliveries:
        raise HTTPException(404, "No verified Master manifest found")
    sidecar, _, payload = deliveries[0]
    return FileResponse(
        str(sidecar),
        media_type="application/json",
        filename=sidecar.name,
        headers={"X-Content-SHA256": content_hash(sidecar), "X-Manifest-ID": str(payload.get("manifest_id", ""))},
    )


@router.get("/{project_id:str}/master-manifest", response_model=MasterManifestInfo)
async def latest_master_manifest(project_id: str) -> MasterManifestInfo:
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")
    deliveries = _valid_master_deliveries(path / "exports")
    if not deliveries:
        raise HTTPException(404, "No verified Master manifest found")
    sidecar, _, payload = deliveries[0]
    export_payload = payload["export"]
    return MasterManifestInfo(
        path=sidecar.name,
        sha256=content_hash(sidecar),
        size=sidecar.stat().st_size,
        manifest_id=str(payload.get("manifest_id", "")),
        created_at=str(payload.get("created_at", "")),
        output_sha256=str(export_payload.get("sha256", "")),
    )


@router.get("/{project_id:str}/download")
async def download(project_id: str):
    try:
        path = safe_project_path(project_id, load_settings().output_base)
    except ValueError:
        raise HTTPException(404, "Project not found")

    project = open_project(path, load_settings())
    if not project:
        raise HTTPException(404, "Project not found")
    exports_dir = project.exports_dir
    if not exports_dir.is_dir():
        raise HTTPException(404, "No exports found")

    state = project.stage_state("export")
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    latest: Path | None = None
    if state.get("status") == "completed":
        configured = Path(str(metadata.get("path", "") or ""))
        if configured and configured.name and not configured.is_absolute():
            configured = exports_dir / configured
        try:
            configured = configured.resolve()
        except OSError:
            pass
        if (
            configured.suffix.lower() == ".mp4"
            and configured.is_file()
            and is_within(configured, exports_dir)
        ):
            if metadata.get("mode") != "master":
                latest = configured
            else:
                verified_outputs = {output for _, output, _ in _valid_master_deliveries(exports_dir)}
                if configured in verified_outputs:
                    latest = configured

    if latest is None:
        deliveries = _valid_master_deliveries(exports_dir)
        if deliveries:
            latest = deliveries[0][1]

    # Read-only compatibility for projects produced before stage metadata and
    # delivery manifests existed. Never use this fallback after a known failure.
    if latest is None and state.get("status") == "completed" and not metadata:
        try:
            legacy = sorted(exports_dir.glob("*.mp4"), key=lambda item: item.stat().st_mtime_ns)
        except OSError:
            legacy = []
        latest = legacy[-1] if legacy else None
    if latest is None:
        raise HTTPException(404, "No verified successful MP4 export found")
    return FileResponse(
        str(latest),
        media_type="video/mp4",
        filename=latest.name,
    )
