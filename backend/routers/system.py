from __future__ import annotations

import platform
import sys
from pathlib import Path

from fastapi import APIRouter, Response
from pydantic import BaseModel

from core.config import load_settings
from core.project import CURRENT_MANIFEST_SCHEMA_VERSION, CURRENT_PROJECT_SCHEMA_VERSION
from core.version import __version__
from backend.services.comfy_watchdog import watchdog_status


router = APIRouter()


class HealthResponse(BaseModel):
    status: str


class ReadinessCheck(BaseModel):
    ok: bool
    message: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, ReadinessCheck]


class BuildInfoResponse(BaseModel):
    app: str
    version: str
    python: str
    platform: str
    project_schema: int
    manifest_schema: int
    runtime_mode: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Cheap liveness probe: importing and routing the API is enough."""
    return HealthResponse(status="ok")


@router.get("/readiness", response_model=ReadinessResponse)
async def readiness(response: Response) -> ReadinessResponse:
    """Local runtime readiness without contacting optional external engines."""
    settings = load_settings()
    output_base = Path(settings.output_base or "projects").expanduser()
    output_parent = output_base if output_base.exists() else output_base.parent
    static_index = Path(__file__).resolve().parents[1] / "static" / "index.html"
    checks = {
        "project_storage": ReadinessCheck(
            ok=output_parent.exists() and output_parent.is_dir(),
            message="project storage parent is available" if output_parent.is_dir() else "project storage parent is missing",
        ),
        "frontend": ReadinessCheck(
            ok=static_index.is_file(),
            message="frontend bundle is present" if static_index.is_file() else "frontend bundle is not built",
        ),
    }
    ready = all(check.ok for check in checks.values())
    if not ready:
        response.status_code = 503
    return ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)


@router.get("/build-info", response_model=BuildInfoResponse)
async def build_info() -> BuildInfoResponse:
    return BuildInfoResponse(
        app="Комбайн Studio",
        version=__version__,
        python=platform.python_version(),
        platform=sys.platform,
        project_schema=CURRENT_PROJECT_SCHEMA_VERSION,
        manifest_schema=CURRENT_MANIFEST_SCHEMA_VERSION,
        runtime_mode="local-single-process",
    )


@router.get("/system/comfy-watchdog")
async def comfy_watchdog_state() -> dict:
    """Current bounded supervisor state; this endpoint never mutates ComfyUI."""

    return watchdog_status()
