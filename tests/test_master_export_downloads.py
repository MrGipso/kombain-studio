from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.routers import export_router
from core import export_manifest
from core.config import Settings
from core.project import content_hash, create_project


def _publish_master_pair(project, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, dict]:
    output = project.exports_dir / "master_verified.mp4"
    output.write_bytes(b"verified master bytes")
    monkeypatch.setattr(
        export_manifest,
        "tool_contract",
        lambda _ffmpeg: {"application": "pipeline-studio", "ffmpeg": {"version": "test"}},
    )
    info = export_manifest.write_master_export_manifest(
        output,
        project_id=project.dir.name,
        project_name=project.name,
        project_schema_version=project.schema_version,
        provenance={
            "ok": True,
            "variant_manifest": {"schema_version": 2, "revision": 3},
            "cards": [],
        },
        profile={"name": "master", "width": 1920, "height": 1080},
        settings_contract={"export_loudnorm": True},
        technical_qc={"ok": True, "errors": []},
        created_at="2026-07-12T10:00:00.000Z",
    )
    return output, output.parent / info["path"], info


def test_download_manifest_skips_invalid_sidecars_and_rejects_tampered_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    settings = Settings(output_base=str(tmp_path))
    project = create_project("Manifest download", tmp_path, settings=settings)
    output, sidecar, info = _publish_master_pair(project, monkeypatch)
    # A newer-looking orphan must never outrank a verified delivery pair.
    (project.exports_dir / "zzz_orphan.mp4.manifest.json").write_text(
        json.dumps({
            "manifest_type": export_manifest.MASTER_MANIFEST_TYPE,
            "manifest_id": "forged",
            "export": {
                "path": "missing.mp4",
                "sha256": "0" * 64,
                "size": 123,
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(export_router, "load_settings", lambda: settings)

    with TestClient(app) as client:
        response = client.get(f"/api/projects/{project.dir.name}/download-manifest")
        assert response.status_code == 200
        assert response.json()["manifest_id"] == info["manifest_id"]
        assert response.content == sidecar.read_bytes()
        assert response.headers["x-content-sha256"] == content_hash(sidecar)
        assert response.headers["x-manifest-id"] == info["manifest_id"]

        manifest_info = client.get(
            f"/api/projects/{project.dir.name}/master-manifest"
        )
        assert manifest_info.status_code == 200
        assert manifest_info.json() == info

        original_sidecar = sidecar.read_bytes()
        altered = json.loads(original_sidecar)
        altered["export"]["profile"]["width"] = 640
        sidecar.write_text(json.dumps(altered), encoding="utf-8")
        rejected_sidecar = client.get(
            f"/api/projects/{project.dir.name}/download-manifest"
        )
        assert rejected_sidecar.status_code == 404
        sidecar.write_bytes(original_sidecar)

        output.write_bytes(b"tampered after publication")
        rejected = client.get(f"/api/projects/{project.dir.name}/download-manifest")

    assert rejected.status_code == 404
    assert rejected.json()["detail"] == "No verified Master manifest found"


@pytest.mark.parametrize("export_state", ["pending", "qc_failed"])
def test_download_never_falls_back_to_orphan_or_qc_failed_raw_mp4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, export_state: str,
):
    settings = Settings(output_base=str(tmp_path))
    project = create_project(f"Raw export {export_state}", tmp_path, settings=settings)
    # Persist an explicit pending manifest before the raw FFmpeg artifact appears;
    # this distinguishes an orphan from a supported pre-manifest legacy export.
    project.save_manifest(project.load_manifest())
    raw_output = project.exports_dir / "raw_unpublished.mp4"
    raw_output.write_bytes(b"ffmpeg output without a successful commit")
    if export_state == "qc_failed":
        project.mark_stage_failed(
            "export",
            "Master technical QC failed",
            metadata={
                "path": str(raw_output),
                "technical_qc": {"ok": False, "errors": ["missing audio"]},
            },
        )
    assert project.stage_state("export")["status"] == export_state.replace("qc_", "")
    monkeypatch.setattr(export_router, "load_settings", lambda: settings)

    with TestClient(app) as client:
        response = client.get(f"/api/projects/{project.dir.name}/download")

    assert response.status_code == 404
    assert response.json()["detail"] == "No verified successful MP4 export found"


def test_master_rechecks_inputs_inside_project_job_before_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    media = tmp_path / "shot.png"
    media.write_bytes(b"frame")
    cards = [{
        "card_id": "locked-card",
        "shot_index": 1,
        "status": "ready",
        "approved": True,
        "route": "image",
        "visual_type": "ai_image",
        "image_output": str(media),
        "video_output": "",
        "assigned_media": "",
        "ltx_draft": False,
    }]
    completed_states = {
        stage: {"status": "completed"}
        for stage in ("transcript", "scenes", "shots", "assets", "timeline")
    }
    failures: list[tuple[str, str, dict | None]] = []

    class FakeProject:
        dir = tmp_path
        name = "Locked project"
        schema_version = 1
        settings = SimpleNamespace(ffmpeg="ffmpeg")
        metadata = {}

        @staticmethod
        def read_json(key: str):
            if key == "asset_board":
                return cards
            if key == "timeline":
                return {"voice_start": "00:00:00"}
            raise AssertionError(key)

        @staticmethod
        def stage_states():
            return completed_states

        @staticmethod
        def mark_stage_failed(stage: str, error: str, metadata=None):
            failures.append((stage, error, metadata))

        @staticmethod
        def effective_settings():
            return {"export_loudnorm": True}

    project = FakeProject()
    inside_job = False
    snapshots = 0

    @contextmanager
    def project_job(*_args, **_kwargs):
        nonlocal inside_job
        inside_job = True
        try:
            yield threading.Event()
        finally:
            inside_job = False

    def master_snapshot(*_args, **_kwargs):
        nonlocal snapshots
        snapshots += 1
        if snapshots == 1:
            assert inside_job is False
            return "preflight-fingerprint", {}, {}, completed_states
        assert inside_job is True
        return "changed-under-lock", {}, {}, completed_states

    export_called = False

    def record_export(**_kwargs):
        nonlocal export_called
        export_called = True
        # Returning nothing takes the already-handled "render failed" path, so
        # the assertions stay focused on the re-check itself.
        return None

    clean_validation = {"issues": [], "summary": {"counts": {"error": 0}}}
    monkeypatch.setattr(export_router, "load_settings", lambda: SimpleNamespace(output_base=""))
    monkeypatch.setattr(export_router, "safe_project_path", lambda *_args: tmp_path)
    monkeypatch.setattr(export_router, "open_project", lambda *_args: project)
    monkeypatch.setattr(export_router, "validate_project", lambda _project: clean_validation)
    monkeypatch.setattr(export_router, "write_validation_report", lambda _project: clean_validation)
    monkeypatch.setattr(
        export_router,
        "evaluate_master_provenance",
        lambda *_args: {"ok": True, "cards": [], "reasons": [], "variant_manifest": {}},
    )
    monkeypatch.setattr(export_router, "_master_input_snapshot", master_snapshot)
    monkeypatch.setattr(export_router.jobs, "project_job", project_job)
    monkeypatch.setattr(export_router, "export_video", record_export)

    async def collect_stream() -> str:
        response = await export_router.export("locked-project", "master")
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    payload = asyncio.run(collect_stream())

    # The snapshot is still re-taken under the project lock, and the refreshed
    # finding still reaches the client — that is what keeps the readiness panel
    # honest. It is advisory now, so the render proceeds instead of aborting.
    assert snapshots == 2
    assert export_called is True
    assert '"code": "export_inputs_changed"' in payload
    assert '"code": "master_preflight_changed"' not in payload
