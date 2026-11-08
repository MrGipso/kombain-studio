"""First-run setup wizard support.

The wizard saves values through ``PUT /api/settings`` (same validation as the
Settings page) and checks them through ``/api/diagnostics``; this router only
adds read-only discovery of tools and local services.
"""

from __future__ import annotations

from fastapi import APIRouter

from core.config import load_settings
from core.setup_detect import detect

router = APIRouter()


@router.get("/setup/detect")
def setup_detect():
    # Plain ``def``: filesystem scans and localhost probes block, so FastAPI
    # runs this in its threadpool instead of on the event loop.
    return detect(load_settings()).to_dict()
